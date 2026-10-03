#!/usr/bin/env python3
"""量化"类别可达率"：证据链实际能发射出哪些大类/子类。

动机
----
官方 28 个子类分属 5 个大类：link(3) / firewall(6) / resource(6) / routing(7) /
service(6)。若故障在子类上近似均匀分布，各大类的真值占比应为
link 10.7% / firewall 21.4% / resource 21.4% / routing 25.0% / service 21.4%。

而 Minor 只在**大类也正确**时才可能得分（§12.6.2：大类错了子类直接记 0），
所以"某大类永远发不出来"等于该大类对应的全部真值在 Major 与 Minor 上双双向零分。

本脚本按区域统计：
  1. 判定函数（_firewall_pressure / _firewall_mechanism / _link_category）在
     真实证据上实际命中多少 incident；
  2. 与"均匀先验下的真值占比"对比，算出结构性不可达的比例与由此推出的
     Major 天花板。

用法：python3 f_category_reachability.py [region ...]
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from aiops.evidence.f_stages import (  # noqa: E402
    _firewall_mechanism,
    _firewall_pressure,
    _link_category,
)

#: 均匀先验下各大类的真值占比（子类数 / 28）
PRIOR = {"link": 3 / 28, "firewall": 6 / 28, "resource": 6 / 28,
         "routing": 7 / 28, "service": 6 / 28}
UNREACHABLE_PRIOR = PRIOR["link"] + PRIOR["firewall"]


def scan(base: Path) -> dict:
    me_path = base / "metric_evidence.json"
    if not me_path.is_file():
        return {}
    inc = json.loads(me_path.read_text(encoding="utf-8")).get("incidents") or {}
    rt_path = base / "routing_evidence.json"
    rt = (json.loads(rt_path.read_text(encoding="utf-8")).get("incidents") or {}
          if rt_path.is_file() else {})
    c = collections.Counter()
    for iid, v in inc.items():
        c["n"] += 1
        nodes = v.get("nodes") or {}
        fw = nodes.get("fw")
        events = list((rt.get(iid) or {}).get("events") or [])
        if _firewall_pressure(fw):
            c["fw_pressure"] += 1
            continue
        if _firewall_mechanism(fw, events):
            c["fw_mechanism"] += 1
            continue
        if any(_link_category(pay) for pay in nodes.values()):
            c["link"] += 1
    return c


def main() -> int:
    roots = sys.argv[1:] or [
        "outputs_experiment_f_ep1", "outputs_experiment_f_full",
    ]
    print(f"均匀先验下真值占比: " +
          " ".join(f"{k}={v * 100:.1f}%" for k, v in PRIOR.items()))
    print(f"其中 link+firewall 合计 = {UNREACHABLE_PRIOR * 100:.1f}% —— "
          f"这部分的 Major 与 Minor 在构造上必然为 0\n")
    tot = collections.Counter()
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        for base in sorted(p for p in rp.iterdir() if p.is_dir()):
            c = scan(base)
            if not c:
                continue
            n = c["n"]
            for k, v in c.items():
                if k != "n":
                    tot[k] += v
            tot["n"] += n
            reach = (c["fw_pressure"] + c["fw_mechanism"] + c["link"]) / n
            print(f"  {base.name[:44]:44s} n={n:5d} "
                  f"fw_pressure={c['fw_pressure']:4d} fw_mech={c['fw_mechanism']:4d} "
                  f"link={c['link']:4d}  可达={reach * 100:5.2f}%")
    if tot["n"]:
        reach = (tot["fw_pressure"] + tot["fw_mechanism"] + tot["link"]) / tot["n"]
        print(f"\n合计 n={tot['n']}  实际可达 {reach * 100:.2f}%  "
              f"vs 先验应有 {UNREACHABLE_PRIOR * 100:.1f}%")
        rest = 1.0 - UNREACHABLE_PRIOR
        print(f"=> Major 天花板 ≈ 10 x {rest:.3f} x (可达大类上的准确率)。"
              f"若取 §12.8 反解的 Major=2.19，则可达大类上的准确率 "
              f"≈ {2.19 / 10 / rest * 100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
