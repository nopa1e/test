#!/usr/bin/env python3
"""§41.4 证据信息量探针：证据包里到底有没有判别信息？

背景
----
数据集中**没有任何标签**（已在 §41.6 彻底确认），所以无法离线判断
"LLM 判得准不准"。但可以判断一个**更便宜、且同样能定生死**的问题：

    证据包本身有没有区分度？

如果 9 台设备的证据形态在所有事故里都长得差不多，那 LLM 再强也变不出信息——
喂进去是一团糊，出来的也只能是糊。这个探针就是去证伪这一点。

做法
----
1. 只挑**"干净样本"**：某台设备的 peak_z 显著高于其余 8 台的事故。
   这些是证据最可能含信息的情形；如果连它们都区分不出来，其余更不必谈。
2. 把**完整证据摘要**喂给 LLM（不是几个节点名+分数，而是各设备的全部指标偏离）。
3. **只问一个问题**：哪台设备的异常不能被其他设备解释？依据是哪些具体观测？
4. **人工看它给出的 `reason`**——不是看选对没有（没标签），而是看：
   - 理由是否**因事故而异**、是否指向**具体观测**
   - 还是**高度雷同**（都是那两三句套话）

判据
----
- 理由高度雷同  -> 证据里没信息，**LLM 这条路到此为止**
- 理由因事故而异 -> 有戏，值得搭 §41.2 的第二三级

用法
----
    python3 f_llm_probe.py --regions beida,xian --limit 60 --out f_llm_probe_out.json
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.chdir("/202531630503/lyt/aiops_diagnosis")

API = "http://127.0.0.1:8000/v1/chat/completions"
MODEL = "deepseek-r1-14b"

SYSTEM = (
    "你是一名资深的网络故障诊断专家，熟悉 BGP/OSPF、防火墙、链路与主机资源故障。"
    "你只根据给出的观测证据推理，不臆测未给出的信息。"
)

PROMPT = """下面是一次网络事故的证据摘要。

事故时间窗：{start} ~ {end}
区域：{region}

9 台设备各自的异常证据。`peak_z` 是该指标相对**自身历史基线**的标准化偏离，
跨设备、跨指标可比，越大越异常；`rel` 是相对变化率。

{evidence}

设备连接关系（邻居）：
{topology}

问题：哪一台设备的异常**不能**被其他设备的异常所解释？也就是最可能的根因。

要求：
1. 你的判断必须基于**上面列出的具体观测**（指明是哪个设备的哪个指标、数值多少）。
2. 说明你**排除了哪些设备**，以及为什么。

只输出 JSON：
{{"root_cause": "<设备名>", "reason": "<依据，必须引用具体指标与数值>", "excluded": ["<被排除的设备及理由>"], "confidence": <0到1的小数>}}"""


def call_llm(prompt: str, timeout: int = 300) -> str:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 2048,
    }).encode("utf-8")
    req = urllib.request.Request(API, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]


def parse_json(text: str) -> dict | None:
    """R1 会先输出思维链，再给答案；取最后一个 JSON 对象。"""
    text = re.sub(r"```(?:json)?", "", text)
    best = None
    for m in re.finditer(r"\{", text):
        depth, i = 0, m.start()
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[m.start():i + 1])
                        if isinstance(obj, dict) and "root_cause" in obj:
                            best = obj
                    except json.JSONDecodeError:
                        pass
                    break
            i += 1
    return best


def top_metrics(node_payload: dict, k: int = 3) -> list[tuple[str, float, float]]:
    out = []
    for name, m in ((node_payload or {}).get("metrics") or {}).items():
        if not isinstance(m, dict):
            continue
        try:
            z = float(m.get("peak_z") or 0.0)
        except (TypeError, ValueError):
            continue
        try:
            rel = float(m.get("relative_change") or 0.0)
        except (TypeError, ValueError):
            continue
        out.append((name, z, rel))
    out.sort(key=lambda x: -x[1])
    return out[:k]


def build_evidence(nodes: dict) -> tuple[str, float]:
    """返回 (证据文本, 最大 z 与次大 z 的差距)。"""
    per = {}
    for node, payload in nodes.items():
        per[node] = top_metrics(payload)
    lines = []
    for node in sorted(per):
        ms = per[node]
        if not ms:
            lines.append(f"- {node}: （无指标证据）")
            continue
        body = "; ".join(f"{n} peak_z={z:.1f} rel={r:+.2f}" for n, z, r in ms)
        lines.append(f"- {node}: {body}")
    zs = sorted((m[0][1] for m in per.values() if m), reverse=True)
    gap = (zs[0] - zs[1]) if len(zs) > 1 else 0.0
    return "\n".join(lines), gap


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="beida,xian")
    ap.add_argument("--limit", type=int, default=30, help="每个区域取多少条")
    ap.add_argument("--min-gap", type=float, default=2.0,
                    help="'干净样本'门槛：最强设备的 peak_z 与次强之差")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--artifacts", default="outputs_experiment_f_ep1")
    ap.add_argument("--out", default="f_llm_probe_out.json")
    a = ap.parse_args()

    picked = []
    for region in [r.strip() for r in a.regions.split(",") if r.strip()]:
        dirs = sorted(Path(a.artifacts).glob(f"{region}_*"))
        if not dirs:
            print(f"[skip] 找不到 {region}")
            continue
        d = dirs[0]
        me = json.loads((d / "metric_evidence.json").read_text(encoding="utf-8"))
        inc = me.get("incidents") or {}
        cand = []
        for iid, v in inc.items():
            nodes = v.get("nodes") or {}
            if len(nodes) < 2:
                continue
            ev, gap = build_evidence(nodes)
            cand.append((gap, iid, v, ev, nodes))
        cand.sort(key=lambda x: -x[0])
        print(f"{region}: {len(inc)} 个事故，gap>= {a.min_gap} 的有 "
              f"{sum(1 for c in cand if c[0] >= a.min_gap)} 个；取前 {a.limit}")
        for gap, iid, v, ev, nodes in cand[: a.limit]:
            iw = v.get("incident_window") or ["", ""]
            picked.append({
                "region": region, "iid": iid, "gap": gap,
                "prompt": PROMPT.format(
                    start=iw[0], end=iw[1], region=region,
                    evidence=ev, topology="(见 metric_evidence 的拓扑扩展：9 台互联)"),
            })

    print(f"\n共取 {len(picked)} 条，开始调用 LLM（workers={a.workers}）...")
    t0 = time.time()

    def work(item):
        try:
            txt = call_llm(item["prompt"])
            obj = parse_json(txt)
            return {**{k: item[k] for k in ("region", "iid", "gap")},
                    "parsed": obj, "raw_len": len(txt)}
        except Exception as exc:
            return {**{k: item[k] for k in ("region", "iid", "gap")},
                    "error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(work, picked))

    ok = [r for r in results if r.get("parsed")]
    print(f"完成：{len(ok)}/{len(results)} 条解析成功，耗时 {time.time()-t0:.0f}s")

    # 关键统计：理由的多样性
    reasons = [str(r["parsed"].get("reason") or "") for r in ok]
    picks = [str(r["parsed"].get("root_cause") or "") for r in ok]
    print(f"\n--- 判据多样性 ---")
    print(f"  不同 root_cause 取值: {len(set(picks))} 种 -> {collections.Counter(picks).most_common(9)}")
    print(f"  理由平均长度: {sum(len(x) for x in reasons)/max(1,len(reasons)):.0f} 字")
    print(f"  理由完全相同的组数: "
          f"{sum(1 for _, c in collections.Counter(reasons).items() if c > 1)}")
    # 前 6 条理由原文，人工看是否因事故而异
    print(f"\n--- 前 6 条理由原文（人工判断用）---")
    for r in ok[:6]:
        print(f"  [{r['region']}/{r['iid'][-4:]} gap={r['gap']:.1f}] "
              f"-> {r['parsed'].get('root_cause')}")
        print(f"      {str(r['parsed'].get('reason'))[:260]}")

    Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"\n已写出 {a.out}")


if __name__ == "__main__":
    main()
