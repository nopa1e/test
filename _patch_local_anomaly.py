#!/usr/bin/env python3
"""§43.5 ③：让 `local_anomaly` 保留**绝对量级**，而不是组内 minmax。

现状（`candidate_generator.py:422`）
-----------------------------------
    local_raw = {node: _local_magnitude(slot) ...}   # 无界的 |relative_change|
    local = minmax(local_raw)                        # 组内 minmax -> 绝对量级被抹掉

后果（§43.5 ③ 原文）：
> 「9 台都很平静、只有一台微动」也会被撑成 1.0。
> A 组那个"够异常台数"判据会因此误判。

改法
----
换成保绝对量级的对数饱和映射：

    local = min(1.0, log1p(raw) / log1p(K))        K = 175（zB1 全量 severity 的 p99 量级）

校准（zB1 实测 severity：中位 5 / p90 17.1 / p99 175）：

    raw = 0      -> 0.000     （真的没证据）
    raw = 1      -> 0.134
    raw = 5      -> 0.347     （中位）
    raw = 17.1   -> 0.560     （p90）
    raw = 175    -> 1.000     （p99，封顶）

于是 `local_anomaly >= 0.5` 对应 raw >= ~13.5，
「A 组够异常台数」才有绝对含义；而且 9 台都平静时大家都拿 0.2~0.35，**不会**被撑成 1.0。

副作用检查（已核对）
--------------------
`candidate_generator._filter_top`（:335）用的是 `local_anomaly > 0.0`，
配合饱和映射等价于 `raw > 0`，与它自己的 docstring
"carries no positive support at all" **语义一致**（旧 minmax 会额外丢掉
组内最弱那台，属于尺度带来的副作用）。故不需要改动 _filter_top。
"""
from __future__ import annotations

import os
import sys

ROOT = "/202531630503/lyt/aiops_diagnosis/aiops/evidence"
RS = os.path.join(ROOT, "root_score.py")
CG = os.path.join(ROOT, "candidate_generator.py")

NEW_FUNC = '''

#: ``local_anomaly`` 的饱和尺度常数：raw = ``_local_magnitude``（无界 |relative_change|）。
#: 取 zB1 全量 severity 的 p99 量级（约 175），使 raw=median(5)->0.35、p90(17)->0.56、
#: p99(175)->1.0。见工作文档 §43.5 ③ / §55.4。
LOCAL_SATURATION_K = 175.0


def saturate_local(
    values: Mapping[str, float], k: float = LOCAL_SATURATION_K
) -> dict[str, float]:
    """保**绝对量级**地把 ``_local_magnitude`` 压到 [0, 1]（见 §43.5 ③）。

    与 :func:`minmax` 的关键差别：minmax 算的是**组内**相对值，所以
    「9 台都很平静、只有一台微动」也会被撑成 1.0/0.0，让
    "够异常的设备台数 = local_anomaly>=0.5 的台数" 这个判据误判。
    这里用绝对对数尺度：只有 raw<=0 才给 0，其余按 ``log1p`` 压缩，K 对应 p99。
    """
    import math

    denom = math.log1p(max(float(k), 1e-9)) or 1.0
    out: dict[str, float] = {}
    for key, value in values.items():
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.0
        if not v > 0.0:            # raw<=0 以及 NaN
            out[key] = 0.0
        else:
            out[key] = min(1.0, math.log1p(v) / denom)
    return out
'''


def patch_root_score() -> bool:
    src = open(RS, encoding="utf-8").read()
    if "def saturate_local" in src:
        print("  root_score.py: 已有 saturate_local，跳过")
        return True
    anchor = "    return {key: (value - lo) / (hi - lo) for key, value in values.items()}\n"
    if anchor not in src:
        print("  !! root_score.py 找不到 minmax 的收尾锚点")
        return False
    src = src.replace(anchor, anchor + NEW_FUNC, 1)
    if "\nimport math\n" not in src:
        imp = "from collections.abc import Mapping, Sequence"
        assert imp in src, "找不到 collections.abc 导入"
        src = src.replace(imp, "import math\n\n" + imp, 1)
    open(RS, "w", encoding="utf-8").write(src)
    print("  root_score.py: 已插入 saturate_local + import math")
    return True


def patch_candidate_generator() -> bool:
    src = open(CG, encoding="utf-8").read()
    old_imp = "from .root_score import ROOT_SCORE_WEIGHTS, combine, explain, minmax, rank_priority"
    new_imp = ("from .root_score import (ROOT_SCORE_WEIGHTS, combine, explain, minmax,\n"
               "                          rank_priority, saturate_local)")
    old_call = "        local = minmax(local_raw)\n"
    new_call = ("        # §43.5 ③：保留绝对量级，不用组内 minmax（否则 A 组的\n"
                "        # \"够异常设备台数\" 判据会把「一台微动」也撑成 1.0）。\n"
                "        local = saturate_local(local_raw)\n")
    if "saturate_local(local_raw)" in src:
        print("  candidate_generator.py: 已打过补丁，跳过")
        return True
    if old_imp not in src:
        print("  !! candidate_generator.py 找不到 root_score 的 import 行")
        return False
    if old_call not in src:
        print("  !! candidate_generator.py 找不到 `local = minmax(local_raw)`")
        return False
    src = src.replace(old_imp, new_imp, 1).replace(old_call, new_call, 1)
    open(CG, "w", encoding="utf-8").write(src)
    print("  candidate_generator.py: import 与调用点均已替换")
    return True


if __name__ == "__main__":
    ok = patch_root_score() and patch_candidate_generator()
    sys.exit(0 if ok else 1)
