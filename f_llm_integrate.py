#!/usr/bin/env python3
"""把 LLM 根因判定结果集成进提交文件。

设计要点
--------
1) **净增量**：只在 LLM 与 RootScore top1 不一致处动刀，一致处原样保留。
2) **只动 top5 顺序**：时间窗与 fault_category 一字不改。Dice 匹配只看时间窗，
   所以本改动**只影响 RCA 的排名分**，不影响 AD / Major / Minor。
3) 三种写回模式（用 --mode 选）：
     reorder  : 仅在 LLM 选点在现有 top5 内时，把它提到 rank1（集合不变）
     outside  : 仅当 LLM 选点在 top5 之外时才动（引入新候选，丢掉原 rank5）
     all      : 两种情况都应用（最激进）
4) 逐行自检：时间窗、fault_category、prediction_id 必须零改动。
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys

os.chdir("/202531630503/lyt/aiops_diagnosis")


def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="submissions/submit_fw_win.jsonl")
    ap.add_argument("--llm-glob", default="llm_rootcause_out/*_full.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="all", choices=["reorder", "outside", "all"])
    ap.add_argument("--min-confidence", type=float, default=0.0)
    a = ap.parse_args()

    base = load_jsonl(a.base)
    by_iid = {}
    for r in base:
        pid = r["prediction_id"]
        by_iid[pid[2:] if pid.startswith("f_") else pid] = r
    print(f"基线 {len(base)} 条")

    # ---- 收集 LLM 判定 ----
    llm = {}
    files = sorted(glob.glob(a.llm_glob))
    print(f"LLM 产物 {len(files)} 个文件")
    for f in files:
        try:
            recs = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print(f"  !! 读取失败 {f}: {e}")
            continue
        for r in recs:
            g = r.get("llm") or {}
            rc = g.get("root_cause")
            if not rc:
                continue
            try:
                conf = float(g.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < a.min_confidence:
                continue
            llm[r["iid"]] = {"pick": str(rc).strip(), "conf": conf,
                             "top1": r.get("rootscore_top1"),
                             "top5": r.get("rootscore_top5") or []}
    print(f"可用 LLM 判定 {len(llm)} 条（覆盖基线 {100*len(llm)/len(base):.1f}%）")

    # ---- 集成 ----
    def norm(node: str, region: str) -> str:
        """LLM 返回短名（service-vm-3），提交里是全名（beida-service-vm-3）。"""
        if node.startswith(region + "-"):
            return node
        return f"{region}-{node}"

    out = []
    stat = collections.Counter()
    detail = collections.Counter()
    for r in base:
        r2 = dict(r)
        pid = r["prediction_id"]
        iid = pid[2:] if pid.startswith("f_") else pid
        info = llm.get(iid)
        if info:
            region = pid.split("_")[2]
            pick = norm(info["pick"], region)
            cur = [x["network_element_id"] for x in r["root_cause_top5"]]
            if pick == cur[0]:
                stat["一致-不动"] += 1
            else:
                inside = pick in cur
                apply_it = (a.mode == "all"
                            or (a.mode == "reorder" and inside)
                            or (a.mode == "outside" and not inside))
                if not apply_it:
                    stat["不一致-按模式跳过"] += 1
                else:
                    new = [pick] + [x for x in cur if x != pick]
                    new = new[:5]
                    if not inside:
                        detail["引入 top5 外候选"] += 1
                    else:
                        detail["仅重排（集合不变）"] += 1
                    stat["已应用"] += 1
                    r2["root_cause_top5"] = [
                        {"rank": i + 1, "network_element_id": n} for i, n in enumerate(new)]
        else:
            stat["无 LLM 数据-不动"] += 1
        out.append(r2)

    print("\n" + "=" * 74)
    print(f"集成模式: {a.mode}   置信度门槛: {a.min_confidence}")
    print("=" * 74)
    for k, v in stat.most_common():
        print(f"  {k:<22s} {v:6d} ({100*v/len(base):5.1f}%)")
    if detail:
        print(f"\n  改动细分:")
        for k, v in detail.most_common():
            print(f"    {k:<24s} {v}")

    # ---- 自检 ----
    bad = collections.Counter()
    for b, r in zip(base, out):
        if b["prediction_id"] != r["prediction_id"]: bad["id"] += 1
        if b["start_time"] != r["start_time"] or b["end_time"] != r["end_time"]: bad["时间窗"] += 1
        if b["fault_category"] != r["fault_category"]: bad["类别"] += 1
        t5 = r["root_cause_top5"]
        if len(t5) != 5: bad["top5长度"] += 1
        if [x["rank"] for x in t5] != [1,2,3,4,5]: bad["rank编号"] += 1
        if len({x["network_element_id"] for x in t5}) != 5: bad["top5重复"] += 1
        if set(b.keys()) != set(r.keys()): bad["字段"] += 1
    print(f"\n  自检: {dict(bad) if bad else '全部为 0 通过 ✓'}")

    with open(a.out, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n  已写出 {a.out} ({len(out)} 行)")


if __name__ == "__main__":
    main()
