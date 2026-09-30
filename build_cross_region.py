#!/usr/bin/env python3
"""预计算跨区域一致性证据（CRCS）并落盘，供 candidates 阶段按 flag 读取。

为什么单独一步：``build_candidates`` 是**逐区域**调用的，而 CRCS 必须
**同时看到同一批的全部区域**才能取到同刻 peer 池。所以先扫一遍所有区域
的 node_metrics 算出 CRCS，落盘成每个区域目录下的 ``cross_region_evidence.json``，
候选阶段再按 incident 的时间窗读取即可。

用法：
  python3 build_cross_region.py --workspace /path/to/ws --output-dir <产物目录>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiops.dataset import discover_datasets           # noqa: E402
from aiops.evidence.cross_region import (               # noqa: E402
    build_crcs,
    role_key,
    summarize,
)

#: 参与 CRCS 的指标。cpu_usage 上 br/cr 与时间维相关性最低（Spearman ~0.19），
#: 互补性最好；其余指标留待后续按需扩展。
METRICS = ("cpu_usage",)
#: 节点级归一化后截断的范围（诊断实测：此区间 top1 变动 26.3%、top5 仅 5.8%）
CLAMP_LO, CLAMP_HI = 0.5, 1.5


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--metric", default="cpu_usage")
    a = ap.parse_args()

    datasets = discover_datasets(a.workspace)
    print(f"发现 {len(datasets)} 个区域")

    # (region, role) -> {timestamp: value}
    series: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for ds in datasets:
        path = os.path.join(str(ds.processed_dir), f"node_metrics_{ds.name}.csv")
        if not os.path.exists(path):
            cands = [f for f in os.listdir(str(ds.processed_dir)) if f.startswith("node_metrics")]
            if not cands:
                print(f"  [skip] {ds.region_code}: 无 node_metrics")
                continue
            path = os.path.join(str(ds.processed_dir), cands[0])
        df = pd.read_csv(path, usecols=["timestamp", "node", a.metric])
        for node, g in df.groupby("node"):
            series[(ds.region_code, role_key(node))].update(
                {ts: float(v) for ts, v in zip(g["timestamp"], g[a.metric])}
            )
        print(f"  {ds.region_code}: {df['node'].nunique()} 节点")

    crcs = build_crcs(series)
    print(f"\nCRCS 序列数: {len(crcs)}")
    print(json.dumps(summarize(crcs), ensure_ascii=False, indent=1)[:800])

    # 节点级归一化：除以其自身 CRCS 中位（消除"天生与别人不一样"的业务异质性），
    # 再截断到 [CLAMP_LO, CLAMP_HI]。
    out_by_region: dict[str, dict] = defaultdict(dict)
    for (region, role), pts in crcs.items():
        if not pts:
            continue
        vals = sorted(pts.values())
        med = vals[len(vals) // 2] or 1.0
        norm = {}
        for ts, v in pts.items():
            r = v / med
            norm[ts] = CLAMP_LO if r < CLAMP_LO else (CLAMP_HI if r > CLAMP_HI else r)
        out_by_region[region][role] = norm

    written = 0
    for ds in datasets:
        payload = {
            "dataset": ds.name,
            "region_code": ds.region_code,
            "metric": a.metric,
            "method": "crcs_median_normalized_clamped",
            "clamp": [CLAMP_LO, CLAMP_HI],
            "roles": out_by_region.get(ds.region_code, {}),
        }
        dest = os.path.join(a.output_dir, ds.name)
        os.makedirs(dest, exist_ok=True)
        p = os.path.join(dest, "cross_region_evidence.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        written += 1
    print(f"\n已写出 {written} 份 cross_region_evidence.json -> {a.output_dir}")


if __name__ == "__main__":
    main()
