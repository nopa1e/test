#!/usr/bin/env python3
"""让 LLM 直接判【故障类别】（大类 + 子类）——工作文档 §43 的 B 组实验，此前没有脚本。

为什么单独做这一件事
--------------------
§48 实测：LLM 的**根因**判定已全量接入且**无信号**（RCA 仍在随机线）。
但**类别**是完全另一回事，而且从未测过：

  * 类别目前 ≈ 随机线（Major 21.9% vs 独立假设下 20%）；
  * 类别占 20 分（Major 10 + Minor 10），是**空间最大且完全未开采**的一块；
  * 现有链条判类别靠的是 if-else 证据分支（`_infer_category`），
    LLM 从没被问过类别问题。

与 llm_rootcause.py 的区别
--------------------------
1. 问的是类别，不是根因；
2. 证据里**必须带业务流量**（web/dns/auth）——`service` 这一大类的唯一来源是
   `traffic_flow_metrics`，只看指标证据的话模型永远不可能答出 service；
3. `--api` 可指向跨容器的 vLLM（主容器本地没有 vLLM，实测 127.0.0.1:8000 拒绝连接，
   而 172.23.191.167:8000 通）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import llm_rootcause as LR

TAXONOMY = """link: delay, rate_limit, loss
firewall: acl_drop, rate_limit, port_block, cpu_pressure, default_route_error, rule_order_error
resource: cpu_pressure, memory_pressure, disk_io_pressure, disk_space_low, process_pressure, softirq_pressure
routing: blackhole, bgp_session_down, bgp_route_flap, wrong_static_route, ospf6_neighbor_down, ospf6_cost_anomaly, wrong_default_route
service: dns_down, dns_wrong_record, web_5xx, web_slow, auth_timeout, auth_error"""

SYS = (
    "你是网络运维专家。根据给定的时间窗内各网元的**异常指标证据**与**业务流量证据**，"
    "判断本次故障的故障类别（大类 + 子类）。只输出 JSON，不要解释过程。"
)

TMPL = """区域: {region}
故障时间窗: {start} ~ {end}

各网元异常指标证据（severity 越大越异常）:
{evidence}

业务流量证据（web / dns / auth 等业务面）:
{flow}

拓扑邻接:
{topo}

请判断本次故障的类别。

⚠️ 最重要的判据纪律（务必遵守）：
1. **CPU / 内存 / 磁盘 的升高绝大多数是"故障传播"的结果，不是类别判据。**
   一次路由震荡或防火墙丢包，会让下游一片设备的 CPU 一起飙高。
   如果你的理由主要是"CPU 最高"，那这个答案多半是错的。
2. 先问自己：**最早异常、且能独立解释其余现象的那一个**是什么？
   用那个来定类别，而不是用"数值最大"的那个。
3. 若 10 台设备**几乎同时**异常（时间戳相同），说明有传播，
   应优先在 routing / link / firewall 里找，而不是 resource。
4. resource 只在"**孤立**的主机资源饱和、且没有网络面异常"时才选。

各类别的典型判据：
- routing：BGP/OSPF 邻居 down、路由翻转、静态/默认路由配错
- link：接口丢包、时延、限速（rate_limit）
- firewall：ACL 丢弃、端口封禁、规则顺序、CPU 压力**在 fw 上**
- service：web 5xx、web 慢、DNS 解析失败/错记录、认证超时/失败
- resource：主机 CPU/内存/磁盘/进程/软中断 饱和

类别取值（**必须严格取自下表**，不得自创）:
{taxonomy}

输出格式（严格 JSON，无其他内容）:
{{"major_category": "<大类>", "sub_category": "<子类>", "confidence": <0~1>, "reason": "<20字内>"}}

务必以一行 JSON 作为回答的最后一行。不要输出其他内容。"""


def build_flow_text(fe_inc: dict, max_lines: int = 6) -> str:
    tf = (fe_inc or {}).get("traffic_flow") or {}
    edges = tf.get("edges") or []
    if not edges:
        return "  (无)"
    agg: dict[str, dict] = {}
    for e in edges:
        ft = str(e.get("flow_type") or "?")
        a = agg.setdefault(ft, {"n": 0, "er": 0.0, "to": 0.0, "rq": 0.0, "dp": 0.0})
        a["n"] += 1
        for key, k in (("error_rate_delta", "er"), ("timeout_delta", "to"),
                       ("requests_delta", "rq"), ("duration_p95_delta", "dp")):
            try:
                a[k] = max(a[k], abs(float(e.get(key) or 0.0)))
            except (TypeError, ValueError):
                pass
    lines = []
    for ft, a in sorted(agg.items(), key=lambda kv: -(kv[1]["er"] + kv[1]["to"])):
        lines.append("  - %-9s 边数=%-3d error_rateΔ=%.4f timeoutΔ=%.4f requestsΔ=%.1f dur_p95Δ=%.5f"
                     % (ft, a["n"], a["er"], a["to"], a["rq"], a["dp"]))
    return "\n".join(lines[:max_lines]) or "  (无)"


def parse_cat(text: str) -> dict | None:
    if not isinstance(text, str) or not text:
        return None
    for line in reversed([l.strip() for l in text.splitlines() if l.strip()]):
        line = line.strip("`").strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("major_category"):
            return obj
    m = re.search(r"\{[^{}]*\"major_category\".*?\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and obj.get("major_category"):
                return obj
        except Exception:
            pass
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--region", default="beida")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--save", default=None)
    ap.add_argument("--api", default="http://172.23.191.167:8000/v1/chat/completions")
    ap.add_argument("--max-nodes", type=int, default=10,
                    help="证据里列出的网元数（类别判定建议给全 10 台）")
    a = ap.parse_args()

    LR.API = a.api
    print(f"vLLM: {a.api}")

    d = os.path.join(a.out_dir, [x for x in os.listdir(a.out_dir) if x.startswith(a.region + "_")][0])
    cands = json.load(open(os.path.join(d, "candidates.json"), encoding="utf-8"))
    metric = LR.load_metric_ev(os.path.join(d, "metric_evidence.json"))
    flow = {}
    fp = os.path.join(d, "flow_evidence.json")
    if os.path.exists(fp):
        flow = (json.load(open(fp, encoding="utf-8")).get("incidents") or {})
    topo = json.load(open(os.path.join(d, "topology.json"), encoding="utf-8"))
    adj: dict[str, list[str]] = {}
    for e in topo.get("edges") or []:
        s, t = str(e.get("source")), str(e.get("target"))
        s = s.split("-", 1)[-1] if "-" in s else s
        t = t.split("-", 1)[-1] if "-" in t else t
        adj.setdefault(s, []).append(t)
        adj.setdefault(t, []).append(s)

    incs = cands.get("incidents") or {}
    ids = list(incs)[: a.limit]
    print(f"区域 {a.region}: 取 {len(ids)} 个 incident 送 LLM")
    mi = metric.get("incidents") or {}

    def work(iid: str):
        payload = mi.get(iid) or {}
        p = incs[iid]
        nodes = list(p.get("top10") or p.get("top5") or [])
        fe_inc = flow.get(iid) or {}
        tr = p.get("time_range") or {}
        start = tr.get("start") or p.get("first_anomaly_time")
        end = tr.get("end")
        if not start or not end:
            iw = (fe_inc.get("incident_window")
                  or next((np.get("incident_window") for np in (payload.get("nodes") or {}).values()
                           if np.get("incident_window")), None))
            if iw and len(iw) >= 2:
                start, end = iw[0], iw[1]
        prompt = TMPL.format(
            region=a.region, start=start or "?", end=end or "?",
            evidence=LR.build_evidence_text(payload, max_nodes=a.max_nodes),
            flow=build_flow_text(fe_inc),
            topo=LR.topo_text(adj, nodes),
            taxonomy=TAXONOMY,
        )
        t0 = time.time()
        txt = None
        try:
            txt = LR.ask(prompt, timeout=300)
            got = parse_cat(txt)
        except Exception as exc:
            got = {"error": f"{type(exc).__name__}: {exc}"}
        return {"iid": iid, "llm": got, "elapsed": time.time() - t0,
                "raw": (txt[:800] if isinstance(txt, str) else None)}

    t_all = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(work, ids))
    wall = time.time() - t_all

    ok = [r for r in results if r["llm"] and r["llm"].get("major_category")]
    print(f"\n成功解析: {len(ok)}/{len(results)}   墙钟 {wall:.1f}s")
    if results:
        print(f"  吞吐: {len(results)/max(wall,1e-9):.3f} incident/s   "
              f"（全量 7945 条预计 {7945/max(len(results)/max(wall,1e-9),1e-9)/3600:.1f} 小时）")
    print(f"  平均单条耗时: {sum(r['elapsed'] for r in results)/max(1,len(results)):.1f}s")

    from collections import Counter
    print("\n大类分布:", dict(Counter(r["llm"]["major_category"] for r in ok).most_common()))
    print("子类分布:", dict(Counter(r["llm"]["sub_category"] for r in ok).most_common()))
    print("\n样例（前 6 条）:")
    for r in results[:6]:
        print(" ", r["iid"], r["llm"])

    if a.save:
        os.makedirs(os.path.dirname(a.save) or ".", exist_ok=True)
        with open(a.save, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
        print(f"已写出 {a.save}")


if __name__ == "__main__":
    main()
