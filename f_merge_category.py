#!/usr/bin/env python3
"""把 A 版的「类别」与 B 版的「时间窗/top5」拼装成一个新版本。

用途：final_svc 的类别判定（含 service 类）来自 flow 证据，价值高；但它的
时间窗是未 retime 的原始 5 分钟（第一批最优为 10 分钟，实测 +0.59 已被它吐回）。
本脚本以 B（retime，时长最优）为骨架，仅把 fault_category 换成 A（svc）的判定。

输出 = B 的 prediction_id / start_time / end_time / root_cause_top5
     + A 的 fault_category（按 prediction_id 对齐）

用法：
  python3 f_merge_category.py --base <B.jsonl> --category-from <A.jsonl> --output <out.jsonl>
"""
from __future__ import annotations
import argparse, json, os


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="提供时间窗/top5 的版本（骨架）")
    ap.add_argument("--category-from", dest="cat_from", required=True,
                    help="提供 fault_category 的版本")
    ap.add_argument("--output", required=True)
    a = ap.parse_args()

    base = [json.loads(l) for l in open(a.base, encoding="utf-8") if l.strip()]
    src = {json.loads(l)["prediction_id"]: json.loads(l)
           for l in open(a.cat_from, encoding="utf-8") if l.strip()}

    changed = miss = 0
    out = []
    for r in base:
        k = r["prediction_id"]
        s = src.get(k)
        r2 = dict(r)
        if s is None:
            miss += 1
        else:
            old = r.get("fault_category")
            new = s.get("fault_category")
            if old != new:
                changed += 1
            r2["fault_category"] = new
        out.append(r2)

    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    with open(a.output, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"骨架 {len(base)} 条；类别来源命中 {len(base)-miss}；缺失 {miss}")
    print(f"类别被替换: {changed} 条 ({changed/max(1,len(base))*100:.1f}%)")
    print(f"输出 {a.output}")


if __name__ == "__main__":
    main()
