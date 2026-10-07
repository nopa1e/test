#!/usr/bin/env python3
"""把五判据 LLM 的 top5 **按时间最近**传播到旧基座的提交上 —— 只动排序。

为什么这样做
------------
1. **只动 `root_cause_top5`**：窗口、类别、行数全部一字不动。
   评分里 AD 的匹配只依赖时间窗（Dice）、Major/Minor 只看 `fault_category`，
   所以分数变化 **= 纯粹的 RCA 变化**。这是目前唯一能干净定价排序的手段。
2. **基座必须用旧基座**：`submit_fill2_rean.jsonl`（23.3344）来自 `f_full`/`f_stage2`
   的 7786 条 incident。新基座（zB1/zB2）刚在 §57 实测为 **−2.82**，不能切。
3. **为什么按时间匹配**：两套 incident 由不同代码生成，id 序号不对应
   （f_full 的 beida 640 条 vs zB1 的 631 条），只能按时间窗对齐。
   这与 `f_fill_timeline.py` 克隆"同区域时间最近的已有预测"是同一个规则。
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
from datetime import datetime, timezone

BATCH = {
    "b1": (datetime(2026, 8, 19, tzinfo=timezone.utc), datetime(2026, 9, 3, tzinfo=timezone.utc)),
    "b2": (datetime(2026, 9, 17, tzinfo=timezone.utc), datetime(2026, 9, 25, tzinfo=timezone.utc)),
}


def ts(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def center(r: dict) -> datetime:
    return ts(r["start_time"]) + (ts(r["end_time"]) - ts(r["start_time"])) / 2


def region_of(r: dict) -> str:
    return r["root_cause_top5"][0]["network_element_id"].split("-", 1)[0]


def batch_of(c: datetime) -> str:
    for b, (lo, hi) in BATCH.items():
        if lo <= c < hi:
            return b
    return "?"


def build_anchors(llm_dir: str) -> dict:
    """(region, batch) -> 排序好的 [(中心时间, top5)]"""
    anchors: dict[tuple[str, str], list] = {}
    for root, batch in (("outputs_experiment_f_zB1", "b1"),
                        ("outputs_experiment_f_zB2", "b2")):
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if not os.path.isdir(p) or "_20" not in d:
                continue
            reg = d.split("_")[0]
            cp = os.path.join(p, "candidates.json")
            fp = os.path.join(llm_dir, f"{reg}_{batch}.json")
            if not (os.path.exists(cp) and os.path.exists(fp)):
                continue
            inc = json.load(open(cp, encoding="utf-8")).get("incidents") or {}
            by_iid = {r["iid"]: r for r in json.load(open(fp, encoding="utf-8"))}
            lst = anchors.setdefault((reg, batch), [])
            for iid, v in inc.items():
                r = by_iid.get(iid)
                t5 = (r or {}).get("top5") or []
                if len(t5) < 5:
                    continue
                t = None
                for c in (v.get("candidates") or []):
                    fa = c.get("first_anomaly_time")
                    if fa:
                        try:
                            t = ts(fa)
                            break
                        except Exception:
                            continue
                if t is not None:
                    lst.append((t, t5))
    for k in anchors:
        anchors[k].sort(key=lambda x: x[0])
    return anchors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="submissions/submit_fill2_rean.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--llm-dir", default="llm_rank5_out")
    ap.add_argument("--only-if-differs", action="store_true",
                    help="只替换那些排序确实不同的行（默认也替换相同的，便于核对）")
    a = ap.parse_args()

    anchors = build_anchors(a.llm_dir)
    tot = sum(len(v) for v in anchors.values())
    print(f"锚点合计 {tot} 条")
    print("  锚点分布:", {f"{k[0]}/{k[1]}": len(v) for k, v in sorted(anchors.items())})

    rows = [json.loads(l) for l in open(a.base, encoding="utf-8") if l.strip()]
    print(f"基座 {len(rows)} 行")

    changed = same = noanchor = 0
    for r in rows:
        reg = region_of(r)
        c = center(r)
        lst = anchors.get((reg, batch_of(c)))
        if not lst:
            noanchor += 1
            continue
        times = [t for t, _ in lst]
        i = bisect.bisect_left(times, c)
        cand = [j for j in (i - 1, i) if 0 <= j < len(times)]
        j = min(cand, key=lambda k: abs((times[k] - c).total_seconds()))
        new = [f"{reg}-{n}" for n in lst[j][1]]
        old = [x["network_element_id"] for x in r["root_cause_top5"]]
        if old == new:
            same += 1
            continue
        if a.only_if_differs:
            changed += 1
        else:
            changed += 1
        r["root_cause_top5"] = [{"rank": k, "network_element_id": n}
                                for k, n in enumerate(new, start=1)]

    print(f"  排序被改 {changed} 行；本来就相同 {same} 行；无锚点 {noanchor} 行")
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"已写出 {a.out}")


if __name__ == "__main__":
    main()
