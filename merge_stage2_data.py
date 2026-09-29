#!/usr/bin/env python3
"""把第二批数据（每区域 3 个时间段）合并成每区域一份连续数据集。

第二批按 2~3 天切片下发（09-17 / 09-19 / 09-21 三段共 7 天），但赛题的
"阶段"指的是整批，因此需要拼成连续 7 天再跑。段边界（09-19 04:00、09-21 04:00）
在两段里各出现一次，用整行哈希去重（set[int]，约 8 字节/行）。

输出目录结构对齐第一批：
  <out>/<region>_<span>/<region>_<span>_data/processed/<table>_<span>.csv
表头只保留第一段的一份；末尾统一 EOF。

用法：
  python3 merge_stage2_data.py --src <解压目录> --out /202531630503/lyt/workspace/data2
"""
from __future__ import annotations

import argparse
import glob
import os
import re
from collections import defaultdict

SPAN = "20260917040000_20260924040000"
SEGS = [
    "20260917040000_20260919040000",
    "20260919040000_20260921040000",
    "20260921040000_20260924040000",
]


def merge_one(parts, out_path: str) -> tuple[int, int]:
    seen: set[int] = set()
    n_in = n_kept = 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fo:
        first = True
        for p in parts:
            with open(p, encoding="utf-8-sig", newline="") as fi:
                header = fi.readline()
                if first:
                    fo.write(header)
                    first = False
                for line in fi:
                    n_in += 1
                    if not line.strip():
                        continue
                    h = hash(line)
                    if h in seen:
                        continue
                    seen.add(h)
                    fo.write(line)
                    n_kept += 1
    return n_in, n_kept


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    regions = sorted(
        {re.match(r"([a-z]+)_", os.path.basename(d)).group(1)
         for d in glob.glob(os.path.join(a.src, "*_2026*"))
         if re.match(r"([a-z]+)_", os.path.basename(d))}
    )
    print(f"区域: {regions}\n")

    for region in regions:
        dst_dir = os.path.join(a.out, f"{region}_{SPAN}", f"{region}_{SPAN}_data", "processed")
        os.makedirs(dst_dir, exist_ok=True)
        print(f"===== {region} -> {dst_dir} =====")

        # 收集该区域每个表在 3 段里的文件
        table_parts: dict[str, list[str]] = defaultdict(list)
        for seg in SEGS:
            d = os.path.join(a.src, f"{region}_{seg}", region)
            if not os.path.isdir(d):
                print(f"  [warn] 缺少 {d}")
                continue
            for f in sorted(glob.glob(os.path.join(d, "*.csv"))):
                name = os.path.basename(f)
                # 去掉文件名里的时间段信息，得到表名
                table = re.sub(r"_?\d{14}_\d{14}", "", name).replace(".csv", "")
                table = re.sub(r"__+", "_", table).strip("_")
                table_parts[table].append(f)

        for table, parts in sorted(table_parts.items()):
            out = os.path.join(dst_dir, f"{table}_{SPAN}.csv")
            n_in, n_kept = merge_one(parts, out)
            size = os.path.getsize(out) / 1048576
            print(f"  {table:<32} 段数={len(parts)} 读入={n_in:>9} 保留={n_kept:>9} "
                  f"去重={n_in-n_kept:>7}  {size:8.1f} MB")
    print("\n完成")


if __name__ == "__main__":
    main()
