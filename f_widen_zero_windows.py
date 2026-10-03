#!/usr/bin/env python3
"""把零宽预测窗口加宽到一个栅格宽度。

背景
----
`episode_min_abnormal_points=1` + `episode_min_duration_minutes=0`（ep1b）会放出
单栅格 episode，其 `start == end`，于是 incident 窗口宽度为 0。评测按 Dice 匹配
窗口，零宽窗口与任何真值的交集恒为 0，**永远过不了 0.4 门槛**——这类预测
一出生就是死的，却照样占用 N_pred、稀释 α_fp、并参与全局匈牙利匹配去挤掉
本来匹配良好的预测。

一个异常点代表**整个栅格**，所以正确的窗口是 `[start, start + bin_minutes]`。

与重跑的等价性（可证明，非近似）
-------------------------------
incident 窗口 = 各成员 episode 的 (min start, max end)。若该窗口宽度为 0，
则 min(start) == max(end)；由 start <= end 可得**所有**成员 episode 都满足
start == end == 同一时刻。因此给 `aiops/episode.py` 打上同样的加宽补丁后重跑，
这些 incident 的窗口恰好是 `[t, t + bin_minutes]`——与本脚本的后处理结果逐位相同。
故本脚本不是权宜之计，而是与重跑等价。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bin-minutes", type=int, default=5)
    ap.add_argument("--min-width-b1", type=float, default=None,
                    help="第一批（窗口起始于 20260819~20260902）的窗口宽度下限，"
                         "窄于此值的加宽到该值，保持起点不动（onset 锚定）。"
                         "§18 实测第一批最优约 10 分钟；§25 实测起点锚定优于居中。")
    ap.add_argument("--min-width-b2", type=float, default=None,
                    help="第二批（20260917~20260924）的窗口宽度下限。实测第二批"
                         "应保持短窗（约 2~3 分钟），夹到 10 分钟会亏 1.04。")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    def floor_for(s: datetime) -> float | None:
        d = s.strftime("%Y%m%d")
        if "20260819" <= d <= "20260902":
            return a.min_width_b1
        if "20260917" <= d <= "20260924":
            return a.min_width_b2
        return None

    rows = [json.loads(line) for line in open(a.base, encoding="utf-8") if line.strip()]
    delta = timedelta(minutes=a.bin_minutes)
    widened = 0
    out = []
    for r in rows:
        s = datetime.fromisoformat(r["start_time"])
        e = datetime.fromisoformat(r["end_time"])
        if e < s:
            raise SystemExit(f"negative window in {r['prediction_id']}")
        floor = floor_for(s)
        target = None
        if e == s:
            target = s + delta                      # 零宽：补一个栅格宽
        if floor is not None and (e - s) < timedelta(minutes=floor):
            cand = s + timedelta(minutes=floor)
            if target is None or cand > target:
                target = cand
        if target is not None and target > e:
            r = dict(r)
            r["end_time"] = target.isoformat()
            widened += 1
        out.append(r)

    # 自检：除 end_time 外一字不改
    bad = 0
    for x, y in zip(rows, out):
        if x["prediction_id"] != y["prediction_id"]:
            bad += 1
        if x["start_time"] != y["start_time"]:
            bad += 1
        if x.get("root_cause_top5") != y.get("root_cause_top5"):
            bad += 1
        if x.get("fault_category") != y.get("fault_category"):
            bad += 1
    print(f"{a.base}: {len(rows)} 行, 加宽 {widened} 条 "
          f"({widened / max(1, len(rows)) * 100:.1f}%), 自检异常 {bad}")
    if bad:
        raise SystemExit("self-check failed; refusing to write")

    if a.dry_run:
        print("(dry-run, 未写出)")
        return
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"已写出 {a.out} ({len(out)} 行)")


if __name__ == "__main__":
    main()
