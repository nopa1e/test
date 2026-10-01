"""自监督故障记忆库（Self-supervised Fault Memory）。

设计来源
--------
仿照人类记忆强化（Hebbian / 长时程增强）与 Generative Agents 的
memory stream + top-k retrieval 机制：

  证据指纹 -> 模板匹配(top-k) -> 命中则强化权重 & 提升该类别的置信度
                              -> 未命中则新建模板(低权重起步)

与 VAE 同源的**自监督**性质：不依赖真值标签，只用「同类证据反复出现」
作为强化信号 —— 正如 VAE 不需要标签、靠重构误差学正常分布。
"""
from .fingerprint import build_fingerprint, render_text
from .store import MemoryStore

__all__ = ["build_fingerprint", "render_text", "MemoryStore"]
from .hybrid import NUM_KEYS, NumericScaler, hybrid_similarity, numeric_vector

__all__ += ["numeric_vector", "NumericScaler", "hybrid_similarity", "NUM_KEYS"]
from .naturalize import naturalize, role_cn

__all__ += ["naturalize", "role_cn"]
