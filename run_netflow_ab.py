#!/usr/bin/env python3
"""Run isolated, two-batch NetFlow on/off experiments before judge submission."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
REGIONS = ("beida", "chengdu", "guangzhou", "nanjing", "shanghai", "shenyang", "wuhan", "xian")
BATCHES = {
    "b1": ("data", "20260819040000_20260902040000", 5),
    "b2": ("data2", "20260917040000_20260924040000", 1),
}
STAGES = ("f6_base", "evidence", "flow", "predictive", "candidates", "finalize")


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def current_commit():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def run_stage(arm, arguments):
    if arm == "off" and "--stage" in arguments and arguments[arguments.index("--stage") + 1] == "f6_base":
        from aiops import features
        import pandas as pd
        # The old region/node_type join contributed no usable NetFlow features.
        # This arm reproduces that absence without changing any other model code.
        features._agg_netflow = lambda ds, cfg: pd.DataFrame()
    from run_experiment_f_evidence_agent import main
    return main(arguments)


def job(experiment, arm, batch, region, commit, resume=False):
    workspace, span, minutes = BATCHES[batch]
    out = experiment / arm / batch / region
    out.mkdir(parents=True, exist_ok=resume)
    previous_path = out / "job_status.json"
    previous = json.loads(previous_path.read_text()) if resume and previous_path.exists() else {}
    completed_stages = {s["stage"]: s for s in previous.get("stages", []) if s["returncode"] == 0}
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               MKL_NUM_THREADS="1", LOKY_MAX_CPU_COUNT="1", PYTHONUNBUFFERED="1")
    status = {"arm": arm, "batch": batch, "region": region, "state": "running", "stages": []}
    try:
        for stage in STAGES:
            if current_commit() != commit:
                raise RuntimeError("Repository HEAD changed during the experiment")
            dsout = out / (region + "_" + span)
            required = {
                "f6_base": "incident_candidates.json", "evidence": "metric_evidence.json",
                "flow": "flow_evidence.json", "predictive": "predictive_evidence.json",
                "candidates": "candidates.json",
            }
            artifact = dsout / required[stage] if stage in required else out / "final" / ("predictions_" + region + ".jsonl")
            if stage in completed_stages and artifact.is_file() and artifact.stat().st_size:
                status["stages"].append(completed_stages[stage])
                continue
            # If a stage must be repeated, invalidate all of its downstream stages.
            completed_stages.clear()
            status["stage"] = stage
            save(out / "job_status.json", status)
            command = [
                sys.executable, str(Path(__file__).resolve()), "--run-stage", arm, "--",
                "--workspace", str(ROOT.parent / "workspace" / workspace),
                "--regions", region, "--output-dir", str(out),
                "--artifacts-dir", str(out), "--stage", stage,
                "--bin-minutes", str(minutes), "--episode-max-gap-minutes", str(minutes),
                "--episode-min-duration-minutes", str(minutes), "--episode-min-abnormal-points", "2",
                "--incident-max-affinity-edges", "50000", "--vae-epochs", "40",
                "--torch-threads", "1", "--no-llm-rerank",
                "--max-netflow-rows", "14000000" if stage == "flow" else "0",
            ]
            start = time.monotonic()
            with (out / (stage + ".log")).open("w") as log:
                completed = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                           stderr=subprocess.STDOUT)
            status["stages"].append({"stage": stage, "returncode": completed.returncode,
                                     "seconds": round(time.monotonic() - start, 2)})
            if completed.returncode:
                raise RuntimeError(f"{stage} exited with {completed.returncode}")
            dsout = out / (region + "_" + span)
            required = {
                "f6_base": "incident_candidates.json", "evidence": "metric_evidence.json",
                "flow": "flow_evidence.json", "predictive": "predictive_evidence.json",
                "candidates": "candidates.json",
            }
            artifact = dsout / required[stage] if stage in required else out / "final" / ("predictions_" + region + ".jsonl")
            if not artifact.is_file() or not artifact.stat().st_size:
                raise RuntimeError(f"Missing stage output: {artifact}")
            print(f"{arm}/{batch}/{region}: {stage} complete", flush=True)
        status["state"] = "complete"
    except Exception as exc:
        status["state"] = "failed"
        status["error"] = str(exc)
    save(out / "job_status.json", status)
    return status


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_rows(path, rows):
    with Path(path).open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


def postprocess(experiment, arm):
    out = experiment / arm
    rows = []
    for batch in BATCHES:
        for region in REGIONS:
            path = out / batch / region / "final" / ("predictions_" + region + ".jsonl")
            records = read_rows(path)
            if not records:
                raise RuntimeError(f"No predictions: {path}")
            rows.extend(records)
    write_rows(out / "base.jsonl", rows)
    roots = {span: str(out / batch / "*") for batch, (_, span, _) in BATCHES.items()}
    # Override legacy artifact constants only inside this process, so each arm
    # reads its own generated point scores rather than historical experiments.
    import f_inject_anomalies as inject
    inject.BATCHES = roots
    sys.argv = ["f_inject_anomalies.py", "--base", str(out / "base.jsonl"),
                "--out", str(out / "injected.jsonl"), "--gate", "0.90", "--dedup-dice", "0.95"]
    inject.main()
    import f_fill_timeline as fill
    sys.argv = ["f_fill_timeline.py", "--base", str(out / "injected.jsonl"),
                "--out", str(out / "filled.jsonl"), "--gap-dice", "0.9",
                "--step-factor", "2", "--window-b1", "10", "--window-b2", "3",
                "--max-vacuum-minutes", "5"]
    fill.main()
    import f_reanchor as reanchor
    reanchor.SPANS = {span: (roots[span], 10.0 if batch == "b1" else 3.0)
                      for batch, (_, span, _) in BATCHES.items()}
    reanchor._c.clear()
    sys.argv = ["f_reanchor.py", "--base", str(out / "filled.jsonl"),
                "--out", str(out / "reanchored.jsonl")]
    reanchor.main()
    from f_category_role_order import reorder, ALLOWED_V2
    from aiops.taxonomy import is_valid_category
    from aiops.dataset import official_network_element_ids
    rows = read_rows(out / "reanchored.jsonl")
    seen, regions, batches = set(), set(), set()
    allowed = {node for region in REGIONS for node in official_network_element_ids(region)}
    for row in rows:
        row["root_cause_top5"] = reorder(row["root_cause_top5"], row["fault_category"]["major_category"], ALLOWED_V2)
        pid = row["prediction_id"]
        assert pid not in seen, f"Duplicate prediction: {pid}"
        seen.add(pid)
        start = datetime.fromisoformat(row["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))
        assert start.tzinfo and end.tzinfo and end > start, pid
        top = row["root_cause_top5"]
        assert 1 <= len(top) <= 5 and len({x["network_element_id"] for x in top}) == len(top)
        assert [x["rank"] for x in top] == list(range(1, len(top) + 1))
        assert all(x["network_element_id"] in allowed for x in top), pid
        region = top[0]["network_element_id"].split("-")[0]
        assert all(x["network_element_id"].startswith(region + "-") for x in top)
        assert is_valid_category(row["fault_category"]["major_category"], row["fault_category"]["sub_category"])
        regions.add(region)
        batches.add(start.month)
    assert regions == set(REGIONS) and batches == {8, 9}
    final = out / "submission.jsonl"
    write_rows(final, rows)
    save(out / "submission_manifest.json", {
        "path": str(final), "rows": len(rows), "regions": sorted(regions),
        "sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
        "single_variable": "NetFlow features on/off; all downstream settings identical",
        "historical_best_is_not_the_causal_control": True,
    })
    return {"arm": arm, "rows": len(rows), "path": str(final)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resume", action="store_true", help="Continue only this experiment from its recorded successful stages.")
    args = parser.parse_args()
    output = args.output.resolve()
    assert output.parent == ROOT and output.name.startswith("outputs_experiment_")
    assert not subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True).strip()
    if args.resume and not (output / "experiment_manifest.json").is_file():
        raise RuntimeError("Resume requires an existing experiment manifest")
    output.mkdir(exist_ok=args.resume)
    prior_manifest = json.loads((output / "experiment_manifest.json").read_text()) if args.resume else {}
    commit = current_commit()
    save(output / "experiment_manifest.json", {
        "commit": commit, "previous_commit": prior_manifest.get("commit"),
        "arms": ["off", "on"], "regions": REGIONS,
        "batches": BATCHES, "stages": STAGES, "workers": args.workers,
        "flow_evidence_budget": 14000000, "netflow_feature_budget": 0,
        "llm": False, "postprocess": {"gate": 0.9, "dedup_dice": 0.95,
         "gap_dice": 0.9, "step_factor": 2, "window_b1": 10, "window_b2": 3, "max_vacuum_minutes": 5},
        "inputs": "Raw workspace/data and workspace/data2 only; no other experiment artifacts.",
    })
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(job, output, arm, batch, region, commit, args.resume)
                   for batch in BATCHES for region in REGIONS for arm in ("off", "on")]
        for future in as_completed(futures):
            results.append(future.result())
            save(output / "progress.json", {"completed": len(results), "total": len(futures), "jobs": results})
    if any(x["state"] != "complete" for x in results):
        save(output / "result.json", {"state": "failed", "jobs": results})
        return 1
    submissions = [postprocess(output, arm) for arm in ("off", "on")]
    save(output / "result.json", {"state": "ready_for_evaluation", "submissions": submissions})
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--run-stage":
        raise SystemExit(run_stage(sys.argv[2], sys.argv[4:]))
    raise SystemExit(main())
