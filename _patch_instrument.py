#!/usr/bin/env python3
"""临时插桩：把 `_local_magnitude` 的**原始值**也落盘，用于标定 K。

saturate 在 raw>=K 处截断到 1.0，所以从 `local_anomaly` 反推不出尾部，
K 无法标定（实测 45% 的节点被截断）。这里把 raw 原样存进候选字典，
跑完 candidates 就能看到真实分布，然后一次把 K 定死。
"""
import os

CG = "/202531630503/lyt/aiops_diagnosis/aiops/evidence/candidate_generator.py"
OLD = '''                    "node": node,
                    "sub_scores": sub_scores,
'''
NEW = '''                    "node": node,
                    # 插桩：_local_magnitude 的原始（未饱和）值，用于标定 LOCAL_SATURATION_K。
                    "local_magnitude": float(local_raw.get(node, 0.0)),
                    "sub_scores": sub_scores,
'''

src = open(CG, encoding="utf-8").read()
if '"local_magnitude"' in src:
    print("已插桩，跳过")
else:
    assert OLD in src, "找不到候选字典的锚点"
    open(CG, "w", encoding="utf-8").write(src.replace(OLD, NEW, 1))
    print("已插入 local_magnitude 字段")
