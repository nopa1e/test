#!/usr/bin/env python3
"""在**新证据**上实测 firewall / link 的各个死分支到底能不能命中。

§38.3 声称：
  ① acl_drop 分支构造上不可能命中 —— fw 接口的 drop/error 字段 peak_z 恒为 0；
  ② rate_limit 分支有真信号却被卡住（6.22% 满足 drop_ratio>=0.95 但命中 0 次）。
但 `_link_category` / `_firewall_mechanism` 的注释又说 rate_limit 分支
**已被有意撤除且实测是净损失**。两边口径不一致，而且都没说清
"字段恒 0" 是数据本身没有信号，还是口径写错。

本脚本在 **zB1（Oct 5，含 z 尺度修复的新证据）** 上把每个分支的原始统计量
打出来，直接判定死因。
"""
from __future__ import annotations

import collections
import json
import os
import statistics as st
import sys

sys.path.insert(0, "/202531630503/lyt/aiops_diagnosis")

ROOT = sys.argv[1] if len(sys.argv) > 1 else "outputs_experiment_f_zB1"

DROP_KEYS = ("rx_drop_rate", "tx_drop_rate", "rx_error_rate", "tx_error_rate", "carrier_changes")
RATE_KEYS = ("rx_bytes_rate", "tx_bytes_rate", "rx_packets_rate", "tx_packets_rate")

dirs = sorted(d for d in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, d)) and "_20" in d)
print(f"证据目录: {ROOT}  区域数 {len(dirs)}")

fw_present = 0
inc_total = 0
drop_vals = []
rate_vals = []
drop_nonzero = 0
rate_ge_095 = 0
ipt_events = collections.Counter()
fw_pressure_hits = 0
fw_cpu_peaks = []
per_region = collections.Counter()

for d in dirs:
    root = os.path.join(ROOT, d)
    mpath = os.path.join(root, "metric_evidence.json")
    if not os.path.exists(mpath):
        print(f"  {d}: 无 metric_evidence.json")
        continue
    with open(mpath, encoding="utf-8") as fh:
        me = json.load(fh)
    incs = me.get("incidents") or {}
    rpath = os.path.join(root, "routing_evidence.json")
    re_ = {}
    if os.path.exists(rpath):
        with open(rpath, encoding="utf-8") as fh:
            re_ = json.load(fh)
    rinc = re_.get("incidents") or {}
    n_here = 0
    for iid, payload in incs.items():
        inc_total += 1
        n_here += 1
        fw = (payload.get("nodes") or {}).get("fw")
        if not fw:
            continue
        fw_present += 1
        cpu = ((fw.get("metrics") or {}).get("cpu_usage")) or {}
        p = cpu.get("incident_peak")
        z = cpu.get("peak_z")
        if p is not None:
            try:
                fw_cpu_peaks.append(float(p))
            except (TypeError, ValueError):
                pass
        if p is not None and z is not None:
            try:
                if float(p) >= 10.0 and float(z) >= 8.0:
                    fw_pressure_hits += 1
                    per_region[d.split("_")[0]] += 1
            except (TypeError, ValueError):
                pass
        dz = 0.0
        rd = 0.0
        for _if, m in (fw.get("interfaces") or {}).items():
            if not isinstance(m, dict):
                continue
            for k in DROP_KEYS:
                s = m.get(k) or {}
                try:
                    dz = max(dz, float(s.get("peak_z") or 0.0))
                except (TypeError, ValueError):
                    pass
            for k in RATE_KEYS:
                s = m.get(k) or {}
                try:
                    rd = max(rd, float(s.get("drop_ratio") or 0.0))
                except (TypeError, ValueError):
                    pass
        drop_vals.append(dz)
        rate_vals.append(rd)
        if dz > 0:
            drop_nonzero += 1
        if rd >= 0.95:
            rate_ge_095 += 1
        for e in (rinc.get(iid) or []):
            mn = str(e.get("metric_name") or "")
            if mn.startswith("ipv6_default_route"):
                ipt_events[mn] += 1
    print(f"  {d.split('_')[0]:<11} incidents={n_here}")


def q(vals, name):
    if not vals:
        print(f"{name}: 无数据")
        return
    vals = sorted(vals)
    def pct(p):
        i = min(len(vals) - 1, int(p * (len(vals) - 1)))
        return vals[i]
    print(f"{name}: n={len(vals)} 中位={pct(.5):.4g} p90={pct(.9):.4g} "
          f"p99={pct(.99):.4g} 最大={vals[-1]:.6g} 非零个数={sum(1 for v in vals if v>0)}")


print()
print("=== fw 接口 drop/error 的 peak_z（acl_drop 的判据）===")
q(drop_vals, "  drop_z")
print(f"  drop_z >= 8.0(_LINK_DROP_Z_MIN) 的 incident 数: {sum(1 for v in drop_vals if v>=8)}")
print(f"  drop_z > 0 的 incident 数: {drop_nonzero}")

print()
print("=== fw 接口 rate 的 drop_ratio（rate_limit 的判据）===")
q(rate_vals, "  rate_drop")
print(f"  rate_drop >= 0.95 的 incident 数: {rate_ge_095} "
      f"({rate_ge_095/max(1,len(rate_vals))*100:.2f}%)")

print()
print("=== default_route_error 的判据 ===")
print(f"  routing_evidence 里 ipv6_default_route* 事件: {dict(ipt_events) or '（无）'}")

print()
print("=== fw 自身 CPU（firewall/cpu_pressure 的判据）===")
print(f"  fw 节点出现的 incident 数: {fw_present}/{inc_total}")
q(fw_cpu_peaks, "  fw cpu incident_peak")
print(f"  peak>=10 且 z>=8 命中: {fw_pressure_hits}  ({dict(per_region)})")
