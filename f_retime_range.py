#!/usr/bin/env python3
"""把预测时长夹到 [min_minutes, max_minutes]，只改 end_time。

依据：time_score = max(0, 1 - (d_start + d_end) / 360)，S_AD = 0.7 + 0.3 * time_score。
起点无法移动（受 incident 窗口约束），终点由时长决定——时长统一 5 分钟时
d_end 单项就把时间分压到地板。真值故障时长样例为 8.6 / 9.3 / 13.3 分钟。

只改 end_time，不动 start_time / root_cause_top5 / fault_category，保证单变量对照。
"""
import argparse
import json
import os
from datetime import datetime, timedelta

FMT = "%Y-%m-%dT%H:%M:%S.000+00:00"


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--min-minutes", type=float, default=10.0)
    ap.add_argument("--max-minutes", type=float, default=30.0)
    a = ap.parse_args()

    rows = [json.loads(line) for line in open(a.input, encoding="utf-8") if line.strip()]
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)

    stat = {"extended": 0, "trimmed": 0, "kept": 0}
    with open(a.output, "w", encoding="utf-8") as fh:
        for r in rows:
            start = parse(r["start_time"])
            end = parse(r["end_time"])
            dur = (end - start).total_seconds() / 60.0
            if dur < a.min_minutes:
                end = start + timedelta(minutes=a.min_minutes)
                stat["extended"] += 1
            elif dur > a.max_minutes:
                end = start + timedelta(minutes=a.max_minutes)
                stat["trimmed"] += 1
            else:
                stat["kept"] += 1
            r["end_time"] = end.strftime(FMT)
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"输入 {len(rows)} 条 -> 输出 {a.output}")
    print(f"  拉长到 {a.min_minutes:g} 分钟: {stat['extended']}")
    print(f"  裁剪到 {a.max_minutes:g} 分钟: {stat['trimmed']}")
    print(f"  原样保留            : {stat['kept']}")


if __name__ == "__main__":
    main()
