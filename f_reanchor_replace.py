#!/usr/bin/env python3
"""把 f_reanchor 的"追加变体"改成"替换窗口"——行数不变，因此没有行数惩罚。

动机（2026-10-05 实测）
------------------------
`submit_fill2_rean.jsonl` = 最佳提交 + 52829 条重锚变体 = 108613 行，实测 **23.3344**，
比基线 23.2556 高 **+0.079**。但多出来的 52829 行本身要付出代价：
  * α_fp 那一项约 −0.095（len 从 55784 涨到 108613）；
  * 全局最大权匹配里的置换损失（§47.4 实测填充行斜率 9.41e-6/行）。
如果能**保持行数不变**还拿到对齐收益，就不必付这两笔。

做法
----
先用 `f_reanchor.py --only-worse-than <阈值>` 生成"只给对齐差的那些行补变体"的输出；
变体的 prediction_id 形如 `f_REAN_<reg>_<reg>_<原始下标:06d>`，所以可以反推出
**哪些原始行被认为对齐差**。然后：

    输出 = (没拿到变体的原始行) + (所有变体)

即：对齐差的行 -> **被重锚窗口顶替**；对齐好的行 -> **原样保留**。
总行数与输入完全相同。

用法
----
    python3 f_reanchor.py --base submissions/submit_fill2.jsonl \
        --only-worse-than 0.5 --out /tmp/rean_bad.jsonl
    python3 f_reanchor_replace.py --base submissions/submit_fill2.jsonl \
        --rean /tmp/rean_bad.jsonl --out submissions/submit_fill2_repl.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="原始提交（f_reanchor 的 --base）")
    ap.add_argument("--rean", required=True, help="f_reanchor 的输出（原始行 + 变体）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    base = [json.loads(l) for l in open(a.base, encoding="utf-8") if l.strip()]
    rean = [json.loads(l) for l in open(a.rean, encoding="utf-8") if l.strip()]
    n = len(base)
    print(f"原始 {n} 行；f_reanchor 输出 {len(rean)} 行 -> 变体 {len(rean) - n} 条")

    # 变体按「原始下标」反查
    replaced_idx: set[int] = set()
    variants: list[dict] = []
    bad = 0
    for r in rean[n:]:
        pid = r.get("prediction_id", "")
        if not pid.startswith("f_REAN_"):
            bad += 1
            continue
        try:
            idx = int(pid.rsplit("_", 1)[1])
        except ValueError:
            bad += 1
            continue
        if 0 <= idx < n:
            replaced_idx.add(idx)
            variants.append(r)
        else:
            bad += 1
    print(f"  可反查下标的变体 {len(variants)} 条；无法反查 {bad} 条")
    print(f"  被替换的原始行 {len(replaced_idx)} 行")

    kept = [r for i, r in enumerate(base) if i not in replaced_idx]
    out_rows = kept + variants
    print(f"  保留原始 {len(kept)} + 变体 {len(variants)} = {len(out_rows)} 行"
          f"（输入 {n} 行，差 {len(out_rows) - n:+d}）")

    ids = collections.Counter(r["prediction_id"] for r in out_rows)
    dup = [k for k, v in ids.items() if v > 1]
    print(f"  重复 prediction_id: {len(dup)}")
    if dup:
        print(f"    e.g. {dup[:3]}")

    with open(a.out, "w", encoding="utf-8") as fh:
        for r in out_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  已写出 {a.out}")


if __name__ == "__main__":
    main()
