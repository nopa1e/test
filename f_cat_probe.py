#!/usr/bin/env python3
"""类别边际探针：把所有记录的大类统一设成某一个大类，量出真值的边际分布。

为什么这样能测
--------------
`fault_category` 既不影响匹配（只看时间窗）、也不影响 top5，所以
「只改类别」的提交，Δ 分数 = 纯 Major + Minor 变化。

如果真值的 routing 占比是 p，那么「全判 routing」的 Major 正确率 ≈ p。
把它和基线（我们现在那套）一比，就能知道**往哪个大类偏移是有利的**。

用法: python3 f_cat_probe.py --base submissions/submit_fill2_rean.jsonl \
          --major routing --sub bgp_session_down --out submissions/probe_routing.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--major", required=True)
    ap.add_argument("--sub", required=True)
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.base, encoding="utf-8") if l.strip()]
    before = collections.Counter()
    for r in rows:
        before[(r.get("fault_category") or {}).get("major_category")] += 1
        r["fault_category"] = {"major_category": a.major, "sub_category": a.sub}
    print(f"基线 {len(rows)} 行；原大类分布 {dict(before.most_common())}")
    print(f"全部改为 {a.major}/{a.sub}")
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"已写出 {a.out}")


if __name__ == "__main__":
    main()
