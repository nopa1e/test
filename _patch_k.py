#!/usr/bin/env python3
"""把 LOCAL_SATURATION_K 从拍脑袋的 175 改成实测的 p99。

实测 `_local_magnitude` 真实分位（zB1+zB2 全量 121158 个节点）：
    p25 = 5.06   p50 = 65.8   p75 = 2669   p90 = 13556   p99 = 197362   最大 = 2.4e6

原来的 175 是按**单个 metric 的 severity** 定的，而 `_local_magnitude` 是
**跨指标/跨模态取最大**，分布高两个数量级。改成 p99 = 197362 之后：
    local_anomaly >= 0.5   <=>   raw >= 443.6   （真正"明显异常"的绝对水平）

注意：K 对**提交分数没有任何影响**——`saturate` 是单调映射，
RootScore 的组内排序对任何 K 都不变；`_filter_top` 用的 `local_anomaly > 0`
等价于 `raw > 0`，也与 K 无关。K 只决定 A 组"够异常台数"判据的绝对门槛。
"""
import re

RS = "/202531630503/lyt/aiops_diagnosis/aiops/evidence/root_score.py"
src = open(RS, encoding="utf-8").read()

old = "LOCAL_SATURATION_K = 175.0"
new = ("LOCAL_SATURATION_K = 197362.0")

if new in src:
    print("已经是 p99，跳过")
else:
    assert old in src, "找不到 LOCAL_SATURATION_K = 175.0"
    src = src.replace(old, new, 1)
    # 同步更新注释里的校准说明
    src = src.replace(
        "#: 取 zB1 全量 severity 的 p99 量级（约 175），使 raw=median(5)->0.35、p90(17)->0.56、\n"
        "#: p99(175)->1.0。见工作文档 §43.5 ③ / §55.4。",
        "#: 取 `_local_magnitude` 自身分布的 p99（zB1+zB2 实测 197362），使\n"
        "#: \"local_anomaly >= 0.5\" 对应 raw >= 443.6 这个**绝对**水平。\n"
        "#: 注意 175 是错的：那是单个 metric severity 的 p99，而 _local_magnitude\n"
        "#: 是跨指标/跨模态取最大，分布高两个数量级（实测 p50=65.8 p90=13556）。\n"
        "#: 见工作文档 §43.5 ③ / §55.4 / §56。",
        1,
    )
    open(RS, "w", encoding="utf-8").write(src)
    print("已把 LOCAL_SATURATION_K 改为 197362.0（实测 p99）")

import subprocess
print(subprocess.run(["grep", "-n", "LOCAL_SATURATION_K\\|#: 取", RS],
                     capture_output=True, text=True).stdout)
