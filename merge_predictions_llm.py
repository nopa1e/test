"""LLM-driven correlation and merging of incident-level predictions.

Why this exists
---------------
Two mechanisms in the pipeline were supposed to collapse redundant events and
neither does:

* ``aiops.llm_tasks.task_a_incident_correlation`` asks the LLM to merge or split
  incident pairs, but its output (``incident_correlation.json``) is written and
  then read by nothing, and it only ever inspects ``max_pairs`` pairs.
* ``aiops.incident_predictions.merge_adjacent_incidents`` merges incidents closer
  than 5 minutes with intersecting nodes - but incidentization already merged at
  a 30-minute gap, so that rule is a strict subset and never fires.  Measured on
  experiment B it merged 0 of 556 incidents.

Meanwhile the competition rules give strong structure to exploit:

* a real fault lasts 1-30 minutes;
* two faults are never simultaneous;
* consecutive faults are generally at least 20 minutes apart;
* scoring is one-to-one: an unmatched prediction is a false positive, and false
  positives lower Precision, which lowers alpha_fp and costs AD points.

So this script post-processes a finished prediction file:

  stage 1  exact-duplicate time windows collapse deterministically (a hard rule,
           not a judgement call - two predictions with identical intervals in one
           region can never match two different faults).  Rank-1 candidates from
           the discarded members are promoted into the survivor's Top5 so no root
           cause hypothesis is lost.
  stage 2  remaining predictions are clustered by temporal proximity and the LLM
           decides, per cluster, what to merge, what to keep and what to drop,
           under the rules above.

It never touches the running experiment chain: it consumes a prediction file and
writes a new one.

Usage
-----
    python3 merge_predictions_llm.py <predictions_high_conf.jsonl> \
        --scores <llm_anomaly_scores.jsonl> \
        --llm-base-url http://127.0.0.1:8000 --llm-model deepseek-r1-14b \
        --out <merged.jsonl>

Options:
    --gap-minutes 20      temporal proximity used to build clusters
    --workers 8           concurrent LLM calls
    --no-llm              run stage 1 only (deterministic dedupe)
    --dry-run             report what would happen, write nothing
    --limit N             only process the first N clusters (smoke testing)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, "/202131510121/lyt/aiops_diagnosis")

REGIONS = ("beida", "shenyang", "xian", "chengdu", "wuhan", "shanghai", "nanjing", "guangzhou")

# ---------------------------------------------------------------- helpers


def region_of(record: dict[str, Any]) -> str:
    pid = str(record.get("prediction_id", ""))
    for r in REGIONS:
        if r in pid:
            return r
    for c in record.get("root_cause_top5") or []:
        neid = str(c.get("network_element_id", ""))
        for r in REGIONS:
            if neid.startswith(r + "-"):
                return r
    return "unknown"


def parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def load_scores(path: Path) -> dict[str, float]:
    scores: dict[str, float] = {}
    if not path.is_file():
        return scores
    for rec in read_jsonl(path):
        pid = rec.get("prediction_id")
        if pid is None:
            continue
        try:
            scores[str(pid)] = float(rec.get("anomaly_probability") or 0.0)
        except (TypeError, ValueError):
            scores[str(pid)] = 0.0
    return scores


def roots_of(record: dict[str, Any]) -> list[str]:
    return [str(c.get("network_element_id", "")) for c in record.get("root_cause_top5") or []]


# ---------------------------------------------------------------- stage 1


def collapse_exact_windows(
    records: list[dict[str, Any]], scores: dict[str, float]
) -> tuple[list[dict[str, Any]], int, int]:
    """Collapse predictions sharing an identical (region, start, end)."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        groups[(region_of(rec), str(rec.get("start_time")), str(rec.get("end_time")))].append(rec)

    survivors: list[dict[str, Any]] = []
    collapsed = 0
    promoted = 0
    for members in groups.values():
        members = sorted(members, key=lambda r: -scores.get(str(r.get("prediction_id")), 0.0))
        base = json.loads(json.dumps(members[0]))
        old = roots_of(base)
        if len(members) > 1:
            collapsed += len(members) - 1
            support: Counter = Counter()
            best_rank: dict[str, int] = {}
            conf_of: dict[str, float] = {}
            primaries: list[str] = []
            for m in members:
                conf = scores.get(str(m.get("prediction_id")), 0.0)
                for cand in m.get("root_cause_top5") or []:
                    nid = str(cand.get("network_element_id", ""))
                    if not nid:
                        continue
                    support[nid] += 1
                    try:
                        r = int(cand.get("rank") or 99)
                    except (TypeError, ValueError):
                        r = 99
                    best_rank[nid] = min(best_rank.get(nid, 99), r)
                    conf_of.setdefault(nid, conf)
                cands = m.get("root_cause_top5") or []
                if cands:
                    top = str(cands[0].get("network_element_id", ""))
                    if top and top not in primaries:
                        primaries.append(top)
            rest = sorted(
                (n for n in support if n not in primaries),
                key=lambda n: (-support[n], best_rank.get(n, 99), -conf_of.get(n, 0.0)),
            )
            final = (primaries + rest)[:5]
            promoted += sum(1 for n in final if n not in old)
            base["root_cause_top5"] = [{"rank": i, "network_element_id": n} for i, n in enumerate(final, 1)]
        else:
            for i, cand in enumerate(base.get("root_cause_top5") or [], 1):
                cand["rank"] = i
        survivors.append(base)
    return survivors, collapsed, promoted


# ---------------------------------------------------------------- stage 2


def build_clusters(
    records: list[dict[str, Any]],
    gap_minutes: float,
    max_span_minutes: float = 60.0,
) -> list[list[dict[str, Any]]]:
    """Group predictions that are close in time, without single-linkage chaining.

    A plain "next start is within N minutes of the running end" rule chains
    badly: measured on experiment B it produced one 80-member group spanning
    hours, because each consecutive pair was close even though the group as a
    whole was not.  A fault lasts at most 30 minutes and consecutive faults are
    at least 20 minutes apart, so a genuine same-fault group cannot span much
    more than that.  ``max_span_minutes`` caps the total span of one cluster.
    """
    clusters: list[list[dict[str, Any]]] = []
    for region in REGIONS:
        sub = [r for r in records if region_of(r) == region]
        sub.sort(key=lambda r: str(r.get("start_time", "")))
        current: list[dict[str, Any]] = []
        cluster_start: datetime | None = None
        current_end: datetime | None = None
        for rec in sub:
            start = parse_ts(rec.get("start_time"))
            end = parse_ts(rec.get("end_time"))
            if start is None or end is None:
                continue
            if current:
                gap = (start - current_end).total_seconds() / 60.0 if current_end else 0.0
                span = (end - cluster_start).total_seconds() / 60.0 if cluster_start else 0.0
                if gap > gap_minutes or span > max_span_minutes:
                    clusters.append(current)
                    current = []
                    cluster_start = None
                    current_end = None
            current.append(rec)
            if cluster_start is None:
                cluster_start = start
            current_end = end if current_end is None else max(current_end, end)
        if current:
            clusters.append(current)
    return [c for c in clusters if len(c) > 1]


RULES = """赛事规则（硬约束，必须遵守）：
1. 同一时间只会出现一个故障，不会同时出现两个故障。
2. 相邻故障之间的时间间隔一般在 20 分钟以上，通常不超过 1 小时。
3. 单个故障持续 1~30 分钟（不超过 1800 秒）。
4. 评分采用一对一匹配：每条真实故障最多匹配一条预测，每条预测最多匹配一条真实故障。
   无法匹配任何真实故障的预测按误报计算，会拉低 Precision 并扣减异常检测得分。
5. 同一事件中 root_cause_top5 的网元不能重复，rank 从 1 连续。"""

TASKS = """对下面这组候选事件逐组判断：

A. 起止时间完全相同 → action=merge：保留 anomaly_probability 最高的一条，其余丢弃。
   把被丢弃条目的 rank1 候选合并进保留条目的 Top5（最多 5 个），避免丢失根因假设。
B. 一条的 end_time 恰好等于另一条的 start_time，且网元有交集、异常形态相似
   → action=merge：合并为一个事件，时间取并集。
C. 时间重叠且主要网元相同、异常形态相似 → action=merge。
D. 时间间隔小于 20 分钟但网元完全不同且异常形态不同 → 依据规则 1~3 判断哪一条更可能是
   真实故障，其余判 action=drop。
E. 时间间隔大于等于 20 分钟 → 保持独立，action=keep，不要合并。

输出严格 JSON，不要解释：
{"groups":[{"incident_ids":["...","..."],"action":"merge|keep|drop","reason":"简短理由",
  "merged":{"start_time":"ISO8601","end_time":"ISO8601",
            "root_cause_top5":[{"rank":1,"network_element_id":"..."}],
            "fault_category":{"major_category":"...","sub_category":"..."}}}]}

说明：action=merge 时必须给出 merged；action=keep 时表示这些成员全部保留原样；
action=drop 时 incident_ids 里除第一条外其余丢弃。"""


def cluster_key(cluster: list[dict[str, Any]]) -> str:
    """Stable identity for a cluster, used as the decision-cache key."""
    import hashlib

    ids = sorted(str(r.get("prediction_id")) for r in cluster)
    return hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()


def load_decision_cache(path: Path) -> dict[str, Any]:
    """Decisions keyed by cluster signature, so an interrupted run resumes free.

    Null decisions are deliberately NOT cached: a null means the call timed out
    or the JSON did not parse, and treating that as final would poison every
    later run.  Only real decisions are reused.
    """
    cache: dict[str, Any] = {}
    if path.is_file():
        for rec in read_jsonl(path):
            key = rec.get("key")
            decision = rec.get("decision")
            if key and decision is not None:
                cache[str(key)] = decision
    return cache


def save_decision(path: Path, key: str, decision: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "decision": decision}, ensure_ascii=False) + "\n")


def build_prompt(cluster: list[dict[str, Any]], scores: dict[str, float]) -> str:
    from aiops.taxonomy import category_json_schema_text

    members = []
    for rec in cluster:
        members.append({
            "prediction_id": rec.get("prediction_id"),
            "start_time": rec.get("start_time"),
            "end_time": rec.get("end_time"),
            "anomaly_probability": round(scores.get(str(rec.get("prediction_id")), 0.0), 4),
            "root_cause_top5": roots_of(rec),
            "fault_category": rec.get("fault_category"),
        })
    return (
        RULES + "\n\n" + TASKS + "\n\n合法故障类别：\n" + category_json_schema_text()
        + "\n\n待判断的候选事件组：\n" + json.dumps(members, ensure_ascii=False)
    )


def call_llm(prompt: str, cfg: Any, max_tokens: int, timeout: int) -> dict[str, Any] | None:
    """Ask the backend for one decision, with an explicit output cap.

    ``LLMBackend._chat_openai_compatible`` never sends ``max_tokens``, so
    ``cfg.llm_max_new_tokens`` is silently ignored on that backend and the model
    is free to generate up to the server's context limit.  With a reasoning model
    such as DeepSeek-R1 that means a long chain of thought before the JSON, which
    is what makes these calls time out.  This helper posts directly so the cap
    actually applies.  (The pipeline bug itself is noted in the TODO section;
    fixing it there mid-chain would make B inconsistent with C/D/E.)
    """
    import urllib.error
    import urllib.request

    from aiops.llm_diagnoser import _parse_json_object

    url = str(cfg.llm_base_url).rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": cfg.llm_model,
        "messages": [
            {"role": "system", "content": "你是 AIOps 故障事件相关性判定模块，只输出严格 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
    }
    if int(max_tokens) > 0:
        # max_tokens <= 0 means "do not send the field" and let the server apply
        # its own limit (here --max-model-len 8192).  For a reasoning model that
        # is the safe setting: any cap tight enough to speed things up also risks
        # cutting the reply off before the JSON block appears.
        payload["max_tokens"] = int(max_tokens)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=int(timeout)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"  !! LLM HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:200]}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"  !! LLM call failed: {exc}", file=sys.stderr)
        return None
    try:
        content = body["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, AttributeError):
        return None
    return _parse_json_object(content)


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-driven prediction merging.")
    ap.add_argument("input")
    ap.add_argument("--scores", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--gap-minutes", type=float, default=20.0)
    ap.add_argument("--max-span-minutes", type=float, default=60.0,
                    help="Maximum total span of one cluster; stops single-linkage chaining.")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--llm-base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--llm-model", default="deepseek-r1-14b")
    ap.add_argument("--llm-timeout", type=int, default=1800,
                    help="Per-call timeout.  A reasoning model on this GPU can take 150-400s "
                         "per cluster under concurrency; a short timeout turns into a silent "
                         "null-decision run.")
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="Output cap per call.  0 (default) omits the field and lets the server "
                         "use its own limit, which is the safe choice for a reasoning model.")
    ap.add_argument("--no-llm", action="store_true", help="stage 1 only")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cache", default=None,
                    help="Decision cache path (default: <out>.decisions.jsonl). "
                         "Makes an interrupted run resumable at no LLM cost.")
    args = ap.parse_args()

    src = Path(args.input).resolve()
    if not src.is_file():
        print(f"file not found: {src}")
        return 1
    scores_path = Path(args.scores) if args.scores else src.parent / "llm_anomaly_scores.jsonl"
    out_path = Path(args.out) if args.out else src.parent / (src.stem + ".merged.jsonl")

    records = read_jsonl(src)
    scores = load_scores(scores_path)
    print(f"input  : {src}  ({len(records)} records)")
    print(f"scores : {scores_path}  ({len(scores)} entries)")

    # ---- stage 1
    survivors, collapsed, promoted = collapse_exact_windows(records, scores)
    print(f"\n[stage 1] 完全相同时间窗去重: {len(records)} -> {len(survivors)}"
          f"  (合并 {collapsed} 条, 提升 {promoted} 个根因候选)")

    if args.no_llm:
        merged = survivors
    else:
        clusters = build_clusters(survivors, args.gap_minutes, args.max_span_minutes)
        if args.limit:
            clusters = clusters[: args.limit]
        total_members = sum(len(c) for c in clusters)
        sizes = sorted((len(c) for c in clusters), reverse=True)
        print(f"[stage 2] 时间邻近聚类(gap<={args.gap_minutes:g} 分钟, 跨度<={args.max_span_minutes:g} 分钟):"
              f" {len(clusters)} 组, 覆盖 {total_members}/{len(survivors)} 条"
              f" ({100 * total_members / max(1, len(survivors)):.0f}%), 最大组 {sizes[0] if sizes else 0} 条")

        from aiops.config import PipelineConfig

        cfg = PipelineConfig(
            workspace="/202131510121/lyt/workspace",
            output_dir="/tmp",
            llm_backend="openai_compatible",
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            llm_tool_calling=False,
            llm_timeout_seconds=args.llm_timeout,
        )

        by_id = {str(r.get("prediction_id")): r for r in survivors}
        consumed: set[str] = set()
        replaced: dict[str, dict[str, Any]] = {}
        stats: Counter = Counter()

        cache_path = Path(args.cache) if args.cache else out_path.with_suffix("").with_suffix("").parent / (out_path.stem + ".decisions.jsonl")
        cache = load_decision_cache(cache_path)
        keys = [cluster_key(c) for c in clusters]
        pending = [(i, clusters[i], keys[i]) for i in range(len(clusters)) if keys[i] not in cache]
        print(f"    决策缓存 {cache_path.name}: 命中 {len(clusters) - len(pending)}/{len(clusters)}，"
              f"需调用 {len(pending)}")

        def apply_decision(raw: Any) -> None:
            if not isinstance(raw, dict):
                stats["llm_unparsed"] += 1
                return
            for grp in raw.get("groups") or []:
                if not isinstance(grp, dict):
                    continue
                ids = [str(x) for x in (grp.get("incident_ids") or [])]
                ids = [i for i in ids if i in by_id]
                if not ids:
                    stats["unknown_ids"] += 1
                    continue
                action = str(grp.get("action", "")).lower()
                if action == "merge" and isinstance(grp.get("merged"), dict):
                    m = grp["merged"]
                    base = json.loads(json.dumps(by_id[ids[0]]))
                    if m.get("start_time"):
                        base["start_time"] = m["start_time"]
                    if m.get("end_time"):
                        base["end_time"] = m["end_time"]
                    rc = m.get("root_cause_top5")
                    if isinstance(rc, list):
                        fixed, seen = [], set()
                        for c in rc:
                            if not isinstance(c, dict):
                                continue
                            nid = str(c.get("network_element_id", ""))
                            if nid and nid not in seen:
                                seen.add(nid)
                                fixed.append({"rank": len(fixed) + 1, "network_element_id": nid})
                            if len(fixed) == 5:
                                break
                        if fixed:
                            base["root_cause_top5"] = fixed
                    cat = m.get("fault_category")
                    if isinstance(cat, dict) and cat.get("major_category") and cat.get("sub_category"):
                        base["fault_category"] = {
                            "major_category": str(cat["major_category"]),
                            "sub_category": str(cat["sub_category"]),
                        }
                    replaced[ids[0]] = base
                    for i in ids[1:]:
                        consumed.add(i)
                    stats["merged"] += 1
                elif action == "drop":
                    for i in ids[1:]:
                        consumed.add(i)
                    stats["dropped"] += 1
                else:
                    stats["kept"] += 1

        # Cached decisions first so they count even if the LLM phase is cut short.
        for i, _c, key in [(i, clusters[i], keys[i]) for i in range(len(clusters)) if keys[i] in cache]:
            apply_decision(cache[key])

        if pending:
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = {pool.submit(call_llm, build_prompt(c, scores), cfg,
                                       args.max_tokens, args.llm_timeout): (i, key)
                           for i, c, key in pending}
                done = 0
                for fut in as_completed(futures):
                    i, key = futures[fut]
                    done += 1
                    if done % 25 == 0 or done == len(pending):
                        print(f"    LLM progress {done}/{len(pending)}", flush=True)
                    raw = fut.result()
                    save_decision(cache_path, key, raw)   # persist before applying
                    apply_decision(raw)

        merged = []
        for rec in survivors:
            pid = str(rec.get("prediction_id"))
            if pid in consumed:
                continue
            merged.append(replaced.get(pid, rec))
        print(f"    结果: {stats}")

    before = Counter(region_of(r) for r in records)
    after = Counter(region_of(r) for r in merged)
    print()
    print("%-12s %8s %8s %8s" % ("region", "before", "after", "removed"))
    print("-" * 42)
    for r in sorted(before):
        print("%-12s %8d %8d %8d" % (r, before[r], after.get(r, 0), before[r] - after.get(r, 0)))
    print("-" * 42)
    print("%-12s %8d %8d %8d" % ("TOTAL", len(records), len(merged), len(records) - len(merged)))

    p0, p1 = 292 / max(1, len(records)), 292 / max(1, len(merged))
    print(f"\nPrecision 上限 {p0:.4f} -> {p1:.4f}   alpha_fp {0.7 + 0.3 * p0:.4f} -> {0.7 + 0.3 * p1:.4f}")

    ids = [r["prediction_id"] for r in merged]
    bad = sum(1 for r in merged
              if [c.get("rank") for c in r.get("root_cause_top5") or []]
              != list(range(1, len(r.get("root_cause_top5") or []) + 1)))
    dup = sum(1 for r in merged
              if len({c.get("network_element_id") for c in r.get("root_cause_top5") or []})
              != len(r.get("root_cause_top5") or []))
    print(f"校验: id 唯一={len(set(ids)) == len(ids)}  rank 连续异常={bad}  网元重复异常={dup}")

    if args.dry_run:
        print("\n--dry-run: 未写出文件")
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in merged:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n写出: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
