"""Micro-benchmark: how many torch CPU threads is fastest for this VAE shape?

The F6 run at 1-minute granularity spends ~1.1 s per 512-row batch on a VAE with
hidden=64 / latent=8.  For a model that small the default thread pool (128 cores
here) is almost certainly pure synchronisation overhead.  This measures it.
"""

from __future__ import annotations

import time

import torch
import torch.nn as nn
import torch.nn.functional as F


class VAE(nn.Module):
    def __init__(self, d: int, hidden: int = 64, latent: int = 8) -> None:
        super().__init__()
        self.enc = nn.Sequential(nn.Linear(d, hidden), nn.ReLU())
        self.mu = nn.Linear(hidden, latent)
        self.logvar = nn.Linear(hidden, latent)
        self.dec = nn.Sequential(nn.Linear(latent, hidden), nn.ReLU(), nn.Linear(hidden, d))

    def forward(self, x):
        h = self.enc(x)
        mu, logvar = self.mu(h), self.logvar(h)
        std = torch.exp(0.5 * logvar)
        z = mu + std * torch.randn_like(std)
        return self.dec(z), mu, logvar


def bench(n_threads: int, n_batches: int = 40, batch: int = 512, d: int = 106) -> float:
    torch.set_num_threads(n_threads)
    model = VAE(d)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    x = torch.randn(batch, d)
    # warm-up
    for _ in range(3):
        opt.zero_grad(set_to_none=True)
        r, mu, lv = model(x)
        loss = F.mse_loss(r, x) - 0.5 * torch.mean(1 + lv - mu.pow(2) - lv.exp())
        loss.backward()
        opt.step()
    t0 = time.perf_counter()
    for _ in range(n_batches):
        opt.zero_grad(set_to_none=True)
        r, mu, lv = model(x)
        loss = F.mse_loss(r, x) - 0.5 * torch.mean(1 + lv - mu.pow(2) - lv.exp())
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        opt.step()
    dt = time.perf_counter() - t0
    return dt / n_batches


def main() -> None:
    print("torch", torch.__version__, "default threads", torch.get_num_threads())
    for n in (1, 2, 4, 8, 16, 32):
        per_batch = bench(n)
        print(f"  threads={n:3d}  {per_batch * 1000:8.2f} ms/batch   "
              f"354 batches/epoch -> {per_batch * 354:7.1f} s/epoch")


if __name__ == "__main__":
    main()
