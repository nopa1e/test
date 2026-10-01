#!/usr/bin/env python3
"""预聚合 netflow 的服务维度时序，并输出阈值分布（供数据驱动地定界）。

架构：每个区域**只读一次** netflow，按 (minute, dst_port) 聚合出
packets / bytes / 会话跨度，落盘为轻量 JSON；后续按 incident 窗口取数即可，
避免逐 incident 重读千万行 CSV。

用法：
  python3 build_netflow_service.py --workspace <ws> --output-dir <产物目录> \
      --points-dir <产物目录>          # 用于取 incident 窗口
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict

import numpy as np
import pandas as pd

PORT_SERVICE = {1053: "dns", 80: "web", 8080: "web"}


def region_of(name: str) -> str:
    return name.split("_")[0]


def aggregate(netflow_path: str, ports: dict) -> dict:
    """按 (minute, dst_port) 聚合 packets/bytes/span。"""
    acc: dict = defaultdict(lambda: {"packets": 0.0, "bytes": 0.0, "span_sum": 0.0, "n": 0})
    reader = pd.read_csv(
        netflow_path,
        usecols=["minute_utc", "dst_port", "packets", "bytes", "first_seen", "last_seen"],
        chunksize=2_000_000,
    )
    for chunk in reader:
        chunk = chunk[chunk["dst_port"].isin(ports.keys())]
        if chunk.empty:
            continue
        fs = pd.to_datetime(chunk["first_seen"], errors="coerce")
        ls = pd.to_datetime(chunk["last_seen"], errors="coerce")
        chunk = chunk.assign(span=(ls - fs).dt.total_seconds())
        for (minute, port), g in chunk.groupby(["minute_utc", "dst_port"]):
            k = (str(minute), int(port))
            acc[k]["packets"] += float(g["packets"].sum())
            acc[k]["bytes"] += float(g["bytes"].sum())
            sp = g["span"].dropna()
            if len(sp):
                acc[k]["span_sum"] += float(sp.sum())
                acc[k]["n"] += int(len(sp))
    out: dict = {}
    for (minute, port), v in acc.items():
        out.setdefault(str(port), {})[minute] = {
            "pk": v["packets"],
            "by": v["bytes"],
            "sp": (v["span_sum"] / v["n"]) if v["n"] else 0.0,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--max-chunks", type=int, default=0, help="0=全量")
    a = ap.parse_args()

    roots = sorted(glob.glob(os.path.join(a.workspace, "*_2026*")))
    for root in roots:
        reg = region_of(os.path.basename(root))
        cands = glob.glob(os.path.join(root, "**", "netflow_5tuple_minute_readable*.csv"), recursive=True)
        if not cands:
            print(f"  [skip] {reg}: 无 netflow")
            continue
        path = cands[0]
        size_mb = os.path.getsize(path) / 1048576
        print(f"  {reg}: 聚合中 ({size_mb:.0f} MB) ...", flush=True)
        agg = aggregate(path, PORT_SERVICE)
        # 直接按区域名构造输出目录（不依赖目录是否已存在）
        span = os.path.basename(root).split("_", 1)[1] if "_" in os.path.basename(root) else ""
        dest = os.path.join(a.output_dir, f"{reg}_{span}")
        os.makedirs(dest, exist_ok=True)
        out = os.path.join(dest, "netflow_service.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"region": reg, "port_service": PORT_SERVICE, "series": agg}, fh)
        n_min = sum(len(v) for v in agg.values())
        print(f"        -> {out}  ({n_min} 个 (minute,port) 点)")


if __name__ == "__main__":
    main()
