#!/usr/bin/env python3
"""用 netflow 聚合结果构造一份「flow_evidence.json 格式」的 service 证据。

目的：让第二批（无 traffic_flow_metrics）与第一批走**同一套** service 判定代码
——`_flow_category` 原样消费本文件，无需分叉。

字段映射（netflow -> flow_evidence）
------------------------------------
  dst_port 1053      -> flow_type "dns"
  dst_port 80 / 8080 -> flow_type "web"
  会话跨度比 slow     -> duration_p95_incident / duration_p95_baseline
  每分钟速率比 rate   -> error_rate_incident（仅在速率骤降时置为高值，
                        借 `_flow_category` 的 error 分支表达"服务不可用"）

阈值（数据驱动，见探针统计）：
  slow_ratio > 2.0  -> 延迟类子类
  rate_ratio < 0.5  -> 可用性类子类
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import pandas as pd

PORT_FLOW = {1053: "dns", 80: "web", 8080: "web"}
SLOW_THR = 2.0
RATE_THR = 0.5


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--netflow-dir", dest="nf_dir", required=True,
                    help="含 netflow_service.json 的目录（每区域一个子目录）")
    ap.add_argument("--preds", required=True, help="提供 incident 时间窗的 predictions jsonl")
    ap.add_argument("--output-dir", required=True, help="产物目录（写入各区域的 flow_evidence.json）")
    a = ap.parse_args()

    preds: dict[str, list] = {}
    with open(a.preds, encoding="utf-8") as fh:
        for line in fh:
            x = json.loads(line)
            r = x["prediction_id"].split("_")[2]
            # 关键：flow_evidence 的键必须与 candidates.json / incidents 的
            # incident_id 一致（``INC_...``），而 prediction_id 是 ``f_INC_...``。
            # 首版直接用 prediction_id 导致 _flow_category 全部查不到（命中 0）。
            iid = x["prediction_id"]
            if iid.startswith("f_"):
                iid = iid[2:]
            preds.setdefault(r, []).append(
                (iid, x["start_time"][:19], x["end_time"][:19])
            )

    total_edges = 0
    for f in sorted(glob.glob(os.path.join(a.nf_dir, "*", "netflow_service.json"))):
        o = json.load(open(f, encoding="utf-8"))
        reg = o["region"]
        series = {}
        for port, ser in o["series"].items():
            idx = pd.to_datetime(sorted(ser.keys()))
            series[port] = {
                "pk": pd.Series([ser[k.strftime("%Y-%m-%d %H:%M:%S")]["pk"] for k in idx], index=idx),
                "sp": pd.Series([ser[k.strftime("%Y-%m-%d %H:%M:%S")]["sp"] for k in idx], index=idx),
            }
        incidents = {}
        for iid, s, e in preds.get(reg, []):
            ts, te = pd.Timestamp(s), pd.Timestamp(e)
            dur = max((te - ts).total_seconds() / 60.0, 1.0)
            bs, be = ts - pd.Timedelta(minutes=60), ts - pd.Timedelta(minutes=1)
            edges = []
            for port, d in series.items():
                ip = d["pk"].loc[ts:te].sum()
                bp = d["pk"].loc[bs:be].sum()
                if bp <= 0:
                    continue
                isp = d["sp"].loc[ts:te]; bsp = d["sp"].loc[bs:be]
                isp = isp[isp > 0]; bsp = bsp[bsp > 0]
                base = float(bsp.mean()) if len(bsp) else 0.0
                inc = float(isp.mean()) if len(isp) else 0.0
                rate = (ip / dur) / (bp / 59.0)
                edges.append({
                    "flow_type": PORT_FLOW.get(int(port), "web"),
                    "port": int(port),
                    "requests_per_min_incident": ip / dur,
                    "requests_per_min_baseline": bp / 59.0,
                    "error_rate_incident": 1.0 if rate < RATE_THR else 0.0,
                    "timeout_per_min_incident": 0.0,
                    "duration_p95_incident": inc,
                    "duration_p95_baseline": base if base > 0 else None,
                    "recv_rate_ratio": rate,
                    "span_ratio": (inc / base) if base > 0 else None,
                })
            if edges:
                total_edges += len(edges)
                incidents[iid] = {
                    "incident_window": [s, e],
                    "traffic_flow": {"edges": edges, "source": "netflow_rebuilt"},
                }
        dest = os.path.join(a.output_dir, f"{reg}_20260917040000_20260924040000")
        os.makedirs(dest, exist_ok=True)
        out = os.path.join(dest, "flow_evidence.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"dataset": f"{reg}_20260917040000_20260924040000", "region": reg,
                       "incidents": incidents, "incident_count": len(incidents),
                       "built_from": "netflow_5tuple"}, fh, ensure_ascii=False)
        print(f"  {reg}: {len(incidents)} incidents, {sum(len(v['traffic_flow']['edges']) for v in incidents.values())} edges -> {out}")

    print(f"\n合计 edges: {total_edges}")


if __name__ == "__main__":
    main()
