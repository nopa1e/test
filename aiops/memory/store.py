"""记忆库：BERT 编码 + 向量索引 + top-k 检索 + 自监督权重强化。

自监督强化规则（不依赖真值标签）
--------------------------------
每个「模板」= 一条被记住的指纹，带一个权重 w，初始 1.0：

    retrieval_score = cosine_sim * (1 + alpha * log1p(hits))

  hits  命中次数（有相似样本落进该模板的半径内）
  新样本 -> 找 top-k 相似模板
    若 max_sim >= tau（视为同一模式）: 该模板 hits += 1，权重上升，
                                       样本继承该模板的类别，置信度 = 加权投票
    否则                          : 新建模板（低权重起步），
                                       类别沿用规则判定的结果

这正对应你说的「符合模板就强化、不符合就新建模板」。强化信号来自
「同类证据反复出现」这一自监督事实，与 VAE 靠重构误差学正常分布同源。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class MemoryItem:
    template_id: str
    text: str
    vector: np.ndarray
    label: str = ""
    sublabel: str = ""
    hits: int = 0
    exemplars: list[str] = field(default_factory=list)   # 命中的 incident_id

    @property
    def weight(self) -> float:
        return 1.0 + math.log1p(self.hits)


class MemoryStore:
    """向量记忆库。CPU-only，用 transformers 直接加载 BERT 编码。"""

    def __init__(self, model_dir: str, *, device: str = "cpu",
                 tau: float = 0.92, top_k: int = 6, alpha: float = 1.0):
        self.model_dir = model_dir
        self.tau = tau
        self.top_k = top_k
        self.alpha = alpha
        self.device = device
        self._tok = None
        self._model = None
        self.items: list[MemoryItem] = []
        self._mat: np.ndarray | None = None

    # ---------- 编码 ----------
    @staticmethod
    def _cgroup_cpus() -> int:
        """容器里 ``os.cpu_count()`` 返回的是**宿主机**核数（实测 100+），
        而本容器 cgroup 只给 4 核。线程开过量会互相争抢，实测把编码速度
        拖到 1.38 秒/条。这里读 cpu.max 拿真实配额。"""
        try:
            q = open("/sys/fs/cgroup/cpu.max").read().split()
            if q and q[0] != "max":
                return max(1, int(int(q[0]) / int(q[1])))
        except Exception:
            pass
        return max(1, min(4, os.cpu_count() or 4))

    def _lazy(self):
        if self._model is None:
            import torch
            from transformers import AutoModel, AutoTokenizer
            self._tok = AutoTokenizer.from_pretrained(self.model_dir)
            self._model = AutoModel.from_pretrained(self.model_dir).to(self.device).eval()
            n = self._cgroup_cpus()
            torch.set_num_threads(n)
            try:
                torch.set_num_interop_threads(1)
            except RuntimeError:
                pass
        return self._tok, self._model

    def encode(self, texts: list[str], batch: int = 64) -> np.ndarray:
        import torch
        tok, model = self._lazy()
        out = []
        with torch.no_grad():
            for i in range(0, len(texts), batch):
                chunk = texts[i:i + batch]
                enc = tok(chunk, padding=True, truncation=True,
                          max_length=256, return_tensors="pt").to(self.device)
                hid = model(**enc).last_hidden_state          # (B, L, H)
                mask = enc["attention_mask"].unsqueeze(-1).float()
                pooled = (hid * mask).sum(1) / mask.sum(1).clamp(min=1e-6)   # mean-pool
                pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
                out.append(pooled.cpu().numpy())
        return np.vstack(out).astype(np.float32)

    # ---------- 记忆写入 ----------
    def add(self, text: str, vector: np.ndarray, label: str = "",
            sublabel: str = "", incident_id: str = "") -> str:
        tid = f"T{len(self.items):05d}"
        self.items.append(MemoryItem(template_id=tid, text=text,
                                     vector=np.asarray(vector, dtype=np.float32),
                                     label=label, sublabel=sublabel,
                                     hits=1,
                                     exemplars=[incident_id] if incident_id else []))
        self._mat = None
        return tid

    def _matrix(self) -> np.ndarray:
        if self._mat is None:
            self._mat = np.vstack([it.vector for it in self.items]) if self.items \
                else np.zeros((0, 384), dtype=np.float32)
        return self._mat

    # ---------- 检索 + 自监督强化 ----------
    def retrieve(self, vector: np.ndarray, k: int | None = None) -> list[tuple[MemoryItem, float]]:
        k = k or self.top_k
        if not self.items:
            return []
        sims = self._matrix() @ np.asarray(vector, dtype=np.float32)
        idx = np.argsort(-sims)[:k]
        return [(self.items[i], float(sims[i])) for i in idx]

    def query(self, vector: np.ndarray, incident_id: str = "",
              fallback_label: str = "", fallback_sublabel: str = "") -> dict:
        """一次记忆查询：匹配 / 强化 / 新建，返回类别与置信度。"""
        hits = self.retrieve(vector)
        if hits and hits[0][1] >= self.tau:
            # 加权投票：相似度 × 模板权重
            votes: dict[str, float] = {}
            # §70：对【大类+子类】这一对投票，而不是"大类投票、子类取最近模板"。
            # 原写法会拼出自相矛盾的组合（例如 resource + web_slow）。
            pair_votes: dict[tuple[str, str], float] = {}
            for it, sim in hits:
                if sim < self.tau * 0.9:
                    continue
                w = sim * self.alpha * it.weight
                votes[it.label] = votes.get(it.label, 0.0) + w
                key = (str(it.label), str(it.sublabel))
                pair_votes[key] = pair_votes.get(key, 0.0) + w
            top = hits[0][0]                            # 最近的那个模板
            if incident_id:
                top.hits += 1
                top.exemplars.append(incident_id)
            if pair_votes:
                label, sublabel = max(pair_votes, key=pair_votes.get)
                # 枚举校验：不在官方 28 子类内则退回最近模板的标签
                try:
                    from ..taxonomy import is_valid_category
                    if not is_valid_category(label, sublabel):
                        label, sublabel = top.label, top.sublabel
                except Exception:
                    pass
            else:
                label, sublabel = top.label, top.sublabel
            total = sum(pair_votes.values()) or 1.0
            conf = pair_votes.get((label, sublabel), 0.0) / total
            return {"label": label, "sublabel": sublabel, "confidence": conf,
                    "matched": True, "template_id": top.template_id,
                    "nearest_sim": hits[0][1], "n_considered": len(hits),
                    "votes": {k: round(v / total, 3) for k, v in votes.items()},
                    "vote_pairs": {f"{m}/{s}": round(v / total, 3)
                                   for (m, s), v in pair_votes.items()}}
        # 未命中 -> 新建模板
        tid = self.add("", vector, fallback_label, fallback_sublabel, incident_id)
        self.items[-1].hits = 0        # 新模板不计命中
        return {"label": fallback_label, "sublabel": fallback_sublabel,
                "confidence": 0.0, "matched": False, "template_id": tid,
                "nearest_sim": hits[0][1] if hits else None,
                "n_considered": len(hits), "votes": {}}

    # ---------- 持久化 ----------
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = {
            "tau": self.tau, "top_k": self.top_k, "alpha": self.alpha,
            "items": [{**{k: v for k, v in it.__dict__.items() if k != "vector"},
                       "vector": it.vector.tolist()} for it in self.items],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)

    def stats(self) -> dict:
        n = len(self.items)
        hit = sum(it.hits for it in self.items)
        return {"templates": n, "total_hits": hit,
                "avg_hits": round(hit / n, 2) if n else 0,
                "max_hits": max((it.hits for it in self.items), default=0)}
