#!/usr/bin/env python3
"""§43.3 的**五判据相对排名** —— 这才是 §43 设计的 LLM 问法，此前从未实现。

之前 `llm_category.py` 是"直接问类别"，等于只做了 §43.4 B 组的后半截；
`llm_rootcause.py` 是"直接问根因"，§48 已实测无信号。本次按设计实现：

七项子分的分工（§43.2）
-----------------------
    temporal_priority      +0.25   程序（时间戳，机械）
    cross_modal_support    +0.20   程序（数证据模态，机械）
    outgoing_propagation   +0.20   LLM
    local_anomaly          +0.20   LLM
    predictive_explanation +0.15   LLM
    incoming_propagation   -0.15   LLM
    contradiction          -0.15   LLM

LLM **只做相对排名，不做绝对打分**（避免它"全部给 0.8"）。
五项判据各得一个名次 r_d(i)，加权平均名次：

    R_d = Σ w_i · r_d(i) / Σ |w_i|          w = (+0.20, +0.20, +0.15, −0.15, −0.15)

按 R_d **升序**取 top5。

⚠️ 负权重项的方向（§43.3 明确警告过）
-------------------------------------
`incoming_propagation` / `contradiction` 的权重是负的。
**提示词里五项必须统一成"1 = 该判据程度最强"，绝不能让 LLM 把负面判据反向排。**
验证：某网元"最容易被别人解释"（incoming 最强）拿到 r=1，
贡献 −0.15×1 = −0.15；而"最不容易被解释"的拿 r=n，贡献 −0.15n（很小的负数）。
前者的 R 更大 ⇒ **排得靠后**，正是我们要的（受害者不该排第一）。
如果提示词把它反向排，这个机制就整个倒过来了。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import llm_rootcause as LR
from llm_category import build_flow_text

CRITERIA = ("local_anomaly", "outgoing_propagation", "predictive_explanation",
            "incoming_propagation", "contradiction")
WEIGHTS = {"local_anomaly": 0.20, "outgoing_propagation": 0.20,
           "predictive_explanation": 0.15, "incoming_propagation": -0.15,
           "contradiction": -0.15}

SYS = ("你是网络运维专家。根据时间窗内各网元的异常证据、业务流量证据与拓扑关系，"
       "对候选网元按给定的五个判据分别给出**相对排名**。只输出 JSON，不要解释过程。")

TMPL = """区域: {region}
故障时间窗: {start} ~ {end}
候选网元（共 {n} 个）: {nodes}

各网元异常指标证据（severity 越大越异常）:
{evidence}

业务流量证据（web / dns / auth 等业务面）:
{flow}

拓扑邻接:
{topo}

请对上述 {n} 个候选网元，按下面五个判据**各给一个从 1 到 {n} 的完整排名**。

⚠️ 方向纪律（务必遵守）：五项**全部**是"程度"排名，
**统一以 1 = 该判据上程度最强**，**不要因为某项是负面判据就反向排**。

1. local_anomaly         —— 该网元**自身**的独立异常强度（与爆炸半径无关）
2. outgoing_propagation  —— 该网元的异常能**解释**多少其他网元的异常（因果上游程度）
3. predictive_explanation—— 用该网元的异常状态能否**预测**其他网元后续的异常
4. incoming_propagation  —— 该网元的异常**能被其他网元解释**的程度（1 = 最容易被解释 = 最像受害者）
5. contradiction         —— 该网元存在**反证**（与"它是根因"相矛盾的现象）的程度

每个判据都必须给出**全部 {n} 个**网元，不许重复、不许遗漏、不许自创名字。

输出格式（严格 JSON，无其他内容）:
{{"local_anomaly": ["<网元>", ...], "outgoing_propagation": [...], "predictive_explanation": [...], "incoming_propagation": [...], "contradiction": [...]}}

务必以一行 JSON 作为回答的最后一行。不要输出其他内容。"""


def parse_rank5(text: str, nodes: list[str]) -> dict | None:
    """从推理模型输出里抠出五个排名数组，并规范成 1..n 的名次表。"""
    if not isinstance(text, str) or not text:
        return None
    obj = None
    for line in reversed([l.strip() for l in text.splitlines() if l.strip()]):
        line = line.strip("`").strip()
        if line.startswith("{"):
            try:
                cand = json.loads(line)
            except Exception:
                continue
            if isinstance(cand, dict) and any(k in cand for k in CRITERIA):
                obj = cand
                break
    if obj is None:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                cand = json.loads(m.group(0))
                if isinstance(cand, dict) and any(k in cand for k in CRITERIA):
                    obj = cand
            except Exception:
                pass
    if obj is None:
        return None
    n = len(nodes)
    idx = {nd: i + 1 for i, nd in enumerate(nodes)}   # 规范名 -> 位置
    out: dict[str, dict[str, int]] = {}
    for crit in CRITERIA:
        seq = obj.get(crit)
        ranks: dict[str, int] = {}
        if isinstance(seq, list):
            r = 1
            for x in seq:
                name = str(x).strip()
                # 允许带区域前缀
                if name not in idx and "-" in name:
                    name = name.split("-", 1)[-1]
                if name in idx and name not in ranks:
                    ranks[name] = r
                    r += 1
        out[crit] = ranks
    return out


def synth(ranks: dict[str, dict[str, int]], nodes: list[str]) -> list[tuple[str, float]]:
    """按 §43.3 的加权平均名次合成，返回升序的 (node, R)。"""
    n = len(nodes)
    denom = sum(abs(w) for w in WEIGHTS.values()) or 1.0
    scored = []
    for nd in nodes:
        tot = 0.0
        for crit, w in WEIGHTS.items():
            r = ranks.get(crit, {}).get(nd)
            if r is None:
                r = n          # 没排到的按最差处理
            tot += w * r
        scored.append((nd, tot / denom))
    scored.sort(key=lambda t: t[1])
    return scored


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--region", default="beida")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--save", default=None)
    ap.add_argument("--api", default="http://172.23.191.167:8000/v1/chat/completions")
    ap.add_argument("--max-nodes", type=int, default=10)
    ap.add_argument("--min-anomalous-devices", type=int, default=0,
                    help="§43.4 A 组：只处理「够异常的设备台数 >= 它」的 incident（16%% 约等于 3）")
    ap.add_argument("--anom-severity-min", type=float, default=1.0,
                    help="判定「够异常」的 severity 门槛")
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
    mi = metric.get("incidents") or {}

    def anomalous_count(iid: str) -> int:
        """§43.6 的原始定义：`local_anomaly >= 0.5` 的**设备台数**。

        注意不能用 severity 代替——§43.1/§43.6 记录的分布
        （1台 54.0% / 2台 30.8% / 3台 10.7% / 4台+ 4.5%，即 >=3台 = 15.2%）
        是按 `local_anomaly` 统计的。用 severity>=1.0 会得到 99.8%，完全跑偏。
        """
        p = incs.get(iid) or {}
        c = 0
        for cand in (p.get("candidates") or []):
            ss = cand.get("sub_scores") or {}
            try:
                if float(ss.get("local_anomaly") or 0.0) >= a.anom_severity_min:
                    c += 1
            except (TypeError, ValueError):
                pass
        if p.get("candidates"):
            return c
        # 退化路径：candidates.json 没有分项时用 severity 兜底
        payload = mi.get(iid) or {}
        c = 0
        for _nd, np_ in (payload.get("nodes") or {}).items():
            best = 0.0
            for _m, st in (np_.get("metrics") or {}).items():
                try:
                    best = max(best, abs(float(st.get("severity") or 0.0)))
                except (TypeError, ValueError):
                    pass
            if best >= 1.0:
                c += 1
        return c

    ids = list(incs)
    if a.min_anomalous_devices > 0:
        before = len(ids)
        ids = [i for i in ids if anomalous_count(i) >= a.min_anomalous_devices]
        print(f"  A 组筛选（够异常设备 >= {a.min_anomalous_devices}，"
              f"severity>={a.anom_severity_min}）: {before} -> {len(ids)} "
              f"({len(ids)/max(1,before)*100:.1f}%)")
    ids = ids[: a.limit]
    print(f"区域 {a.region}: 取 {len(ids)} 个 incident 送 LLM")

    def work(iid: str):
        payload = mi.get(iid) or {}
        p = incs[iid]
        nodes = list(p.get("top10") or p.get("top5") or [])
        nodes = nodes[: a.max_nodes]
        fe_inc = flow.get(iid) or {}
        tr = p.get("time_range") or {}
        start = tr.get("start") or p.get("first_anomaly_time")
        end = tr.get("end")
        if not start or not end:
            iw = (fe_inc.get("incident_window")
                  or next((np_.get("incident_window") for np_ in (payload.get("nodes") or {}).values()
                           if np_.get("incident_window")), None))
            if iw and len(iw) >= 2:
                start, end = iw[0], iw[1]
        prompt = TMPL.format(
            region=a.region, start=start or "?", end=end or "?",
            n=len(nodes), nodes=", ".join(nodes),
            evidence=LR.build_evidence_text(payload, max_nodes=a.max_nodes),
            flow=build_flow_text(fe_inc),
            topo=LR.topo_text(adj, nodes),
        )
        t0 = time.time()
        txt = None
        ranks = None
        try:
            txt = LR.ask(prompt, timeout=420)
            ranks = parse_rank5(txt, nodes)
        except Exception as exc:
            ranks = None
            txt = f"ERROR {type(exc).__name__}: {exc}"
        top5 = []
        if ranks:
            top5 = [nd for nd, _ in synth(ranks, nodes)][:5]
        return {"iid": iid, "ranks": ranks, "top5": top5,
                "rootscore_top5": p.get("top5") or [],
                "n_nodes": len(nodes),
                "elapsed": time.time() - t0,
                "raw": (txt[:600] if isinstance(txt, str) else None)}

    t_all = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(work, ids))
    wall = time.time() - t_all

    ok = [r for r in results if r["ranks"]]
    print(f"\n成功解析: {len(ok)}/{len(results)}   墙钟 {wall:.1f}s")
    if results:
        thr = len(results) / max(wall, 1e-9)
        print(f"  吞吐: {thr:.3f} incident/s")
    print(f"  平均单条耗时: {sum(r['elapsed'] for r in results)/max(1,len(results)):.1f}s")

    if ok:
        from collections import Counter
        print("\ntop1 分布:", dict(Counter(r["top5"][0] for r in ok if r["top5"]).most_common()))
        # 与程序版 RootScore 的一致率
        agree = sum(1 for r in ok if r["top5"] and r["rootscore_top5"]
                    and r["top5"][0] == r["rootscore_top5"][0])
        in5 = sum(1 for r in ok if r["top5"] and r["rootscore_top5"]
                  and r["top5"][0] in r["rootscore_top5"])
        print(f"  五判据 top1 == RootScore top1 : {agree}/{len(ok)} ({agree/len(ok)*100:.1f}%)")
        print(f"  五判据 top1 ∈ RootScore top5  : {in5}/{len(ok)} ({in5/len(ok)*100:.1f}%)")

    print("\n样例（前 5 条）:")
    for r in results[:5]:
        print(" ", r["iid"], "->", r["top5"])
        if r["ranks"]:
            for c in CRITERIA:
                print(f"      {c:<24} {list(r['ranks'][c].items())[:4]}")

    if a.save:
        os.makedirs(os.path.dirname(a.save) or ".", exist_ok=True)
        with open(a.save, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
        print(f"已写出 {a.save}")


if __name__ == "__main__":
    main()
