#!/usr/bin/env python3
"""让 LLM 直接看证据判根因（不同于 rerank：rerank 只在我们的 top5 内重排，
信息量被既有排序锁死；这里是让模型自己从候选池里选，是独立的信息通路）。"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

API = "http://127.0.0.1:8000/v1/chat/completions"
MODEL = "deepseek-r1-14b"

SYS = (
    "你是网络运维专家。根据给定的时间窗内各网元的异常证据与拓扑关系，"
    "判断本次故障最可能的根因网元。只输出 JSON，不要解释过程。"
)

TMPL = """区域: {region}
故障时间窗: {start} ~ {end}

各网元异常证据（severity 越大越异常）:
{evidence}

拓扑邻接:
{topo}

请判断根因网元最可能是哪一个。
候选网元: {nodes}

输出格式（严格 JSON，无其他内容）:
{{"root_cause": "<网元名>", "confidence": <0~1>, "reason": "<20字内>"}}"""


def load_metric_ev(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_evidence_text(payload: dict, max_nodes: int = 5) -> str:
    nodes = payload.get("nodes") or {}
    rows = []
    for node, p in nodes.items():
        mets = p.get("metrics") or {}
        top = sorted(mets.items(), key=lambda kv: -abs(float(kv[1].get("severity") or 0.0)))[:3]
        if not top:
            continue
        parts = [f"{n}{float(v.get('severity') or 0):+.2f}" for n, v in top]
        rows.append((max(abs(float(v.get("severity") or 0.0)) for _, v in top), node, parts))
    rows.sort(reverse=True)
    out = []
    for _, node, parts in rows[:max_nodes]:
        out.append(f"  - {node}: " + ", ".join(parts))
    return "\n".join(out) if out else "  (无)" 


def topo_text(adj: dict, nodes: list[str]) -> str:
    seen = set()
    lines = []
    for n in nodes:
        for m in (adj.get(n) or [])[:3]:
            k = tuple(sorted((n, m)))
            if k in seen:
                continue
            seen.add(k)
            lines.append(f"  {n} <-> {m}")
    return "\n".join(lines[:12]) if lines else "  (未知)"


def ask(prompt: str, timeout: int = 120) -> str:
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 300,
    }
    r = requests.post(API, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse_json(text: str) -> dict | None:
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True, help="产物目录")
    ap.add_argument("--region", default="xian")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--save", default=None)
    a = ap.parse_args()

    d = os.path.join(a.out_dir, [x for x in os.listdir(a.out_dir) if x.startswith(a.region + "_")][0])
    cands = json.load(open(os.path.join(d, "candidates.json"), encoding="utf-8"))
    metric = load_metric_ev(os.path.join(d, "metric_evidence.json"))
    topo = json.load(open(os.path.join(d, "topology.json"), encoding="utf-8"))
    adj = {}
    for e in topo.get("edges") or []:
        s, t = str(e.get("source")), str(e.get("target"))
        s = s.split("-", 1)[-1] if "-" in s else s
        t = t.split("-", 1)[-1] if "-" in t else t
        adj.setdefault(s, []).append(t)
        adj.setdefault(t, []).append(s)

    incs = cands.get("incidents") or {}
    ids = list(incs)[: a.limit]
    print(f"区域 {a.region}: 取 {len(ids)} 个 incident 送 LLM")

    mi = (metric.get("incidents") or {})

    def work(iid: str):
        payload = mi.get(iid) or {}
        p = incs[iid]
        nodes = list(p.get("top10") or p.get("top5") or [])
        tr = p.get("time_range") or {}
        prompt = TMPL.format(
            region=a.region,
            start=tr.get("start") or (p.get("first_anomaly_time") or "?"),
            end=tr.get("end") or "?",
            evidence=build_evidence_text(payload),
            topo=topo_text(adj, nodes),
            nodes=", ".join(nodes),
        )
        t0 = time.time()
        try:
            txt = ask(prompt)
            got = parse_json(txt)
        except Exception as exc:
            got = {"error": f"{type(exc).__name__}: {exc}"}
        return {"iid": iid, "llm": got, "rootscore_top1": (p.get("top5") or [None])[0],
                "rootscore_top5": p.get("top5") or [], "elapsed": time.time() - t0, "raw": txt[:200] if 'txt' in dir() else None}

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(work, ids))

    ok = [r for r in results if r["llm"] and r["llm"].get("root_cause")]
    agree = sum(1 for r in ok if r["llm"]["root_cause"] == r["rootscore_top1"])
    in5 = sum(1 for r in ok if r["llm"]["root_cause"] in (r["rootscore_top5"] or []))
    print(f"\n成功解析: {len(ok)}/{len(results)}")
    print(f"  LLM 根因 == RootScore top1 : {agree} ({agree/max(1,len(ok))*100:.1f}%)")
    print(f"  LLM 根因 ∈ RootScore top5  : {in5} ({in5/max(1,len(ok))*100:.1f}%)")
    print(f"  平均耗时: {sum(r['elapsed'] for r in results)/max(1,len(results)):.1f}s")
    print("\n样例（前 8 条）:")
    for r in results[:8]:
        llm = (r["llm"] or {}).get("root_cause")
        print(f"  {r['iid'][-8:]}  LLM={llm}  RootScore_top1={r['rootscore_top1']}  "
              f"{'一致' if llm == r['rootscore_top1'] else '不一致'}")
    if a.save:
        json.dump(results, open(a.save, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"\n已保存 -> {a.save}")


if __name__ == "__main__":
    main()
