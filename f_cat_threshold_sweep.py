#!/usr/bin/env python3
"""离线扫描类别判定阈值，看大类边际分布怎么变（不用重跑流水线）。

背景（2026-10-05/06 实测）
--------------------------
* 「只换类别」的提交实测差 **−0.886 分**（LLM 类别 vs 我们的类别）——
  说明**类别模块对改动的响应有约 0.9 分**，里面有真结构；
* 而我们的边际分布离 spec 的均匀子类先验很远：

    大类        我们链条    spec 先验
    routing      36.9%      25.0%
    service      33.2%      21.4%
    resource     28.1%      21.4%
    firewall      1.7%      21.4%   <-- 差 12.7 倍
    link          0.0%      10.7%   <-- 几乎不发

* `_FW_CPU_Z_MIN=8.0` / `_LINK_DROP_Z_MIN=8.0` 是在 **z 尺度修复之前**
  标定的；`890fa74` 把 peak_z 从 np.std 换成 MAD+截断后，
  z 的分布整个变了（最大 z 从 8,475,443 变成 200）。**阈值很可能已经失配。**

用法
----
    python3 f_cat_threshold_sweep.py --out-dir outputs_experiment_f_full
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, "/202531630503/lyt/aiops_diagnosis")
from aiops.evidence import f_stages as FS  # noqa: E402


def load(p):
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="outputs_experiment_f_full")
    ap.add_argument("--limit-regions", type=int, default=0)
    a = ap.parse_args()

    dirs = sorted(d for d in os.listdir(a.out_dir)
                  if os.path.isdir(os.path.join(a.out_dir, d)) and "_20" in d)
    if a.limit_regions:
        dirs = dirs[: a.limit_regions]
    print(f"区域数 {len(dirs)}")

    loaded = []
    for d in dirs:
        root = os.path.join(a.out_dir, d)
        incs = load(os.path.join(root, "incident_candidates.json"))
        incs = incs.get("incidents") if isinstance(incs, dict) else incs
        cands = load(os.path.join(root, "candidates.json"))
        rec = {
            "name": d,
            "incs": incs or [],
            "cands": (cands.get("incidents") or {}),
            "routing": load(os.path.join(root, "routing_evidence.json")),
            "metric": load(os.path.join(root, "metric_evidence.json")),
            "flow": load(os.path.join(root, "flow_evidence.json")),
            "base": load(os.path.join(root, "prediction.json")),
        }
        loaded.append(rec)
        print(f"  {d}: incidents={len(rec['incs'])}")

    grid = [(fw, lk) for fw in (8.0, 4.0, 2.0, 0.0) for lk in (8.0, 4.0, 2.0)]
    print(f"\n{'FW_Z':>6}{'LINK_Z':>8}   " + "".join(f"{k:>11}" for k in
          ("routing", "resource", "service", "firewall", "link", "base")))
    for fw, lk in grid:
        FS._FW_CPU_Z_MIN = fw
        FS._LINK_DROP_Z_MIN = lk
        c = collections.Counter()
        for rec in loaded:
            fallback = rec["base"].get("fault_category") or {}
            for inc in rec["incs"]:
                iid = str(inc.get("incident_id"))
                payload = rec["cands"].get(iid) or {}
                order = payload.get("top5") or payload.get("top10") or []
                seen = []
                for nd in order:
                    if nd not in seen:
                        seen.append(nd)
                got = FS._infer_category(rec["routing"], rec["metric"], iid,
                                         seen[0] if seen else "", fallback, rec["flow"])
                c[(got or {}).get("major_category") or "?"] += 1
        tot = sum(c.values()) or 1
        row = "".join(f"{c.get(k,0)/tot*100:10.1f}%" for k in
                      ("routing", "resource", "service", "firewall", "link"))
        print(f"{fw:>6.1f}{lk:>8.1f}   {row}   n={tot}")


if __name__ == "__main__":
    main()
