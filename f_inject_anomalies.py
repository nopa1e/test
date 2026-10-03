#!/usr/bin/env python3
"""把「高分孤立单点异常」补成预测，直接注入提交文件。

动机（实测 2026-10-02）
----------------------
episode 构建要求「>=2 个连续异常点」（episode_min_abnormal_points=2）
且 duration>=min_duration。实测阈值之上的连续段里：

    gap=5min:  多点段 685  |  单点段 1982   (beida)
    第一批合计: 多点段 5120 |  单点段 14911（其中 >=0.95 的 5880）

即 **74% 的高分异常段因为「孤立」而被整体丢弃**，这些异常从未变成预测。

为什么不重跑流水线
------------------
incidentization 是 O(n^2)：原版 1999 episodes 要 52 分钟，
episode 增到 2667（3.9x）就需约 7.6 小时，不可行。
本脚本改为**后处理注入**，不触碰流水线。

收益的非对称性
--------------
AD = 40 x (ΣS_AD / N_true) x α_fp。加预测只会：
  - 让 ΣS_AD 单调不减（max-weight 匹配的边集变大）
  - 让 α_fp = 0.7 + 0.3·tp/len 略降
故最坏情况只损失约 0.1 分；而若新增预测命中了原本漏掉的真故障，
tp 上升会同时抬高 AD / RCA / Major / Minor 四项。
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os

import pandas as pd

os.chdir("/202531630503/lyt/aiops_diagnosis")

BATCHES = {
    "20260819040000_20260902040000": "outputs_experiment_f_full",
    "20260917040000_20260924040000": "outputs_experiment_f_stage2",
}
ROLE_CAT = {
    "fw": ("firewall", "cpu_pressure"),
    "service-vm": ("service", "web_slow"),
    "traffic-vm": ("resource", "cpu_pressure"),
    "br": ("routing", "bgp_session_down"),
    "cr": ("routing", "bgp_session_down"),
    "monitor-vm": ("resource", "cpu_pressure"),
}


def role_of(short: str) -> str:
    for r in ("service-vm", "traffic-vm", "monitor-vm", "br", "cr", "fw"):
        if short.startswith(r):
            return r
    return "br"


def segments(pts: pd.DataFrame, thr: float, gap_sec: int = 300):
    sub = pts[pts["combined_anomaly_score"] >= thr].sort_values(
        ["network_element_id", "timestamp_bin"])
    out = []
    for nid, g in sub.groupby("network_element_id"):
        rows = list(g.itertuples())
        cur = [rows[0]]
        for r in rows[1:]:
            if (r.timestamp_bin - cur[-1].timestamp_bin).total_seconds() <= gap_sec:
                cur.append(r)
            else:
                out.append((nid, cur)); cur = [r]
        out.append((nid, cur))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="submissions/submit_llm_rc.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--gate", type=float, default=0.95, help="单点段的分数门槛")
    ap.add_argument("--dedup-dice", type=float, default=0.5,
                    help="与同区域已有预测的 Dice 超过它则视为重复，不注入")
    a = ap.parse_args()

    base = [json.loads(l) for l in open(a.base, encoding="utf-8")]
    print(f"基线 {len(base)} 条")

    # 每批次的中位窗口宽度（用实测值，不拍脑袋）
    win_by_batch = {}
    for span, _ in BATCHES.items():
        d = []
        for r in base:
            if span not in r["prediction_id"]:
                continue
            s = pd.Timestamp(r["start_time"]).tz_localize(None)
            e = pd.Timestamp(r["end_time"]).tz_localize(None)
            d.append((e - s).total_seconds() / 60)
        win_by_batch[span] = float(pd.Series(d).median()) if d else 10.0
    print(f"各批次中位窗口宽度: {{k[:8]: v for k, v in ...}}".replace("{k[:8]: v for k, v in ...}", str({k[:8]: v for k, v in win_by_batch.items()})))

    # 已有预测的 (region, 区间) 索引，用于去重
    exist = collections.defaultdict(list)
    for r in base:
        region = r["prediction_id"].split("_")[2]
        exist[region].append((pd.Timestamp(r["start_time"]).tz_localize(None),
                              pd.Timestamp(r["end_time"]).tz_localize(None)))

    def dice(s1, e1, s2, e2) -> float:
        """标准 Dice 系数 = 2|A∩B| / (|A|+|B|)。

        修正记录（2026-10-03）：原实现分母误写成并集
        ``|A|+|B|-|A∩B|``（那是 Jaccard 型），会算出 >1 的值
        （完全相同的时间窗得 2.0 而非 1.0）。后果是阈值被隐性放大：
        ``buggy>0.95`` 实际只相当于真 Dice>0.62，**把大量本该注入的预测
        当成了重复丢掉**。
        """
        lo, hi = max(s1, s2), min(e1, e2)
        inter = max(0.0, (hi - lo).total_seconds())
        tot = (e1 - s1).total_seconds() + (e2 - s2).total_seconds()
        return 2 * inter / tot if tot > 0 else 0.0

    new_preds = []
    stat = collections.Counter()
    for span, root in BATCHES.items():
        W = win_by_batch[span]
        for f in sorted(glob.glob(f"{root}/*/point_scores.csv.gz")):
            region = os.path.basename(os.path.dirname(f)).split("_")[0]
            ep = os.path.join(os.path.dirname(f), "episode_scores.csv")
            thr = float(pd.read_csv(ep)["threshold"].iloc[0])
            pts = pd.read_csv(f, usecols=["network_element_id", "timestamp_bin",
                                          "combined_anomaly_score"])
            pts["timestamp_bin"] = pd.to_datetime(pts["timestamp_bin"])
            # 统一成 tz-naive，避免与从提交里解析出的 naive 时间戳比较报错
            if getattr(pts["timestamp_bin"].dt, "tz", None) is not None:
                pts["timestamp_bin"] = pts["timestamp_bin"].dt.tz_localize(None)
            segs = segments(pts, thr)
            for nid, rows in segs:
                if len(rows) != 1:
                    continue
                sc = float(rows[0].combined_anomaly_score)
                if sc < a.gate:
                    stat["单点-低于门槛"] += 1
                    continue
                t = rows[0].timestamp_bin
                s = t - pd.Timedelta(minutes=W / 2)
                e = t + pd.Timedelta(minutes=W / 2)
                if any(dice(s, e, s2, e2) > a.dedup_dice for s2, e2 in exist[region]):
                    stat["单点-与已有重复"] += 1
                    continue
                short = str(nid).split("-", 1)[-1]
                maj, sub = ROLE_CAT.get(role_of(short), ("resource", "cpu_pressure"))
                prefix = str(nid).split("-")[0]
                top5 = [f"{prefix}-{short}"]
                # 同区其它节点按固定顺序补位
                for cand in ("br-1", "br-2", "cr-1", "cr-2", "service-vm-1",
                             "service-vm-2", "service-vm-3", "traffic-vm", "fw"):
                    if cand != short:
                        top5.append(f"{prefix}-{cand}")
                pid = (f"f_AINJ_{region}_{span}_"
                       f"{t.strftime('%Y%m%d%H%M%S')}_{short}")
                new_preds.append({
                    "prediction_id": pid,
                    "start_time": s.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
                    "end_time": e.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
                    "root_cause_top5": [{"rank": i + 1, "network_element_id": n}
                                        for i, n in enumerate(top5[:5])],
                    "fault_category": {"major_category": maj, "sub_category": sub},
                })
                exist[region].append((s, e))
                stat["已注入"] += 1

    print("\n" + "=" * 74)
    print(f"注入门槛 score>={a.gate}   与已有预测去重 Dice>{a.dedup_dice}")
    print("=" * 74)
    for k, v in stat.most_common():
        print(f"  {k:<22s} {v:6d}")
    print(f"\n  新增预测 {len(new_preds)} 条  ->  总行数 {len(base) + len(new_preds)}")

    ids = {r["prediction_id"] for r in base}
    dup = [p for p in new_preds if p["prediction_id"] in ids]
    print(f"  prediction_id 冲突: {len(dup)}")

    out = base + new_preds
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n  已写出 {a.out} ({len(out)} 行)")


if __name__ == "__main__":
    main()
