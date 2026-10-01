"""混合检索：BERT 语义相似度 + 连续数值特征距离。

动机（实测）
------------
纯文本指纹有三个问题：
  1) 640 条 incident 只有 363 种不同文本 —— 43% 的形态完全相同，
     BERT 相似度饱和在 1.000，无法区分；
  2) 离散成 5 档（none/low/mid/high/extreme）丢掉了数值差异，
     cpu_peak=12 和 cpu_peak=49 落到同一档；
  3) 自然语言化能把区间从 0.027 拉到 0.071，但仍不够。

因此把「连续数值」单独做成一个特征向量，与 BERT 向量加权融合：
    sim = w_sem * cos(bert_a, bert_b) + w_num * exp(-||num_a - num_b||)
数值向量做 robust 归一化（中位数/IQR），避免尖峰把尺度拉爆。
"""
from __future__ import annotations

import numpy as np

#: 参与连续距离的指纹字段（role -> 指标 key）
NUM_KEYS = ("cpu_peak", "cpu_z", "relative_change")


def numeric_vector(fp: dict, roles=("fw", "br", "cr", "service-vm", "traffic-vm")) -> list[float]:
    """抽出连续数值特征。缺失填 0（代表"无异常"而非"未知"）。"""
    out: list[float] = []
    per = fp.get("per_node") or {}
    for role in roles:
        agg = {}
        for name, m in per.items():
            if not name.startswith(role):
                continue
            for k in NUM_KEYS:
                v = m.get(k)
                if v is None:
                    continue
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                agg[k] = max(agg.get(k, 0.0), v)
        for k in NUM_KEYS:
            out.append(float(agg.get(k, 0.0)))
    out.append(float(fp.get("duration_min") or 0))
    out.append(float(fp.get("episode_count") or 0))
    rt = fp.get("routing") or {}
    out.append(float(rt.get("n_events") or 0))
    out.append(1.0 if rt.get("real") else 0.0)
    fl = fp.get("flow") or {}
    out.append(float(sum(1 for v in fl.values() if v not in ("none", None))))
    return out


class NumericScaler:
    """robust 归一化：用中位数与 IQR，避免注入尖峰支配尺度。"""

    def __init__(self):
        self.med = None
        self.iqr = None

    def fit(self, X: np.ndarray) -> "NumericScaler":
        self.med = np.median(X, axis=0)
        q75 = np.percentile(X, 75, axis=0)
        q25 = np.percentile(X, 25, axis=0)
        self.iqr = np.where((q75 - q25) < 1e-9, 1.0, q75 - q25)
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        Z = (X - self.med) / self.iqr
        Z = np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0)
        # 逐样本 L2 归一化，使 exp(-d) 有界且在 [0,1]
        n = np.linalg.norm(Z, axis=1, keepdims=True)
        return Z / np.clip(n, 1e-9, None)


def hybrid_similarity(bert_q: np.ndarray, num_q: np.ndarray,
                      BERT: np.ndarray, NUM: np.ndarray,
                      w_sem: float = 0.5) -> np.ndarray:
    """返回每条记忆与 query 的融合相似度（0~1）。"""
    sem = BERT @ bert_q                                  # 已 L2 归一化 -> 余弦
    d = np.linalg.norm(NUM - num_q[None, :], axis=1)     # 归一化后欧氏距离
    num = np.exp(-d)
    return w_sem * sem + (1.0 - w_sem) * num
