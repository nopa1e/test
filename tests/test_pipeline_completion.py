"""Partial regional failures must never look like a successful aggregate run."""
from pathlib import Path
import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from aiops import pipeline
from aiops.config import PipelineConfig
from aiops.dataset import DatasetInfo
import run_experiment_f_evidence_agent as experiment_f

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs_experiment_code_optimization_20261011"


class PipelineCompletionTests(unittest.TestCase):
    def setUp(self):
        OUTPUT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=OUTPUT)
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.cfg = PipelineConfig(output_dir=self.out)
        self.datasets = [DatasetInfo(name, name, self.out) for name in ["beida", "xian", "wuhan"]]

    def run_with_failures(self, failed):
        def one(ds, cfg, **kwargs):
            if ds.name in failed:
                raise ValueError("injected regional failure")
            return {"dataset": ds.name, "prediction": {"prediction_id": kwargs["prediction_id"]}}, {}
        with patch.object(pipeline, "discover_datasets", return_value=self.datasets), \
             patch.object(pipeline, "run_one_dataset", side_effect=one) as run:
            if failed:
                with self.assertRaisesRegex(RuntimeError, "dataset"):
                    pipeline.run_all(self.out, self.cfg)
            else:
                result = pipeline.run_all(self.out, self.cfg)
                self.assertEqual(len(result["results"]), 3)
            self.assertEqual(run.call_count, 3)
        return json.loads((self.out / "_all" / "run_summary.json").read_text())

    def test_success_preserves_prediction_order(self):
        summary = self.run_with_failures(set())
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["failed_datasets"], [])
        rows = [json.loads(line) for line in (self.out / "_all" / "predictions.jsonl").read_text().splitlines()]
        self.assertEqual([row["prediction_id"] for row in rows], ["pred_000001", "pred_000002", "pred_000003"])

    def test_partial_failure_records_all_regions_and_raises(self):
        summary = self.run_with_failures({"xian"})
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["failed_datasets"], ["xian"])
        self.assertEqual(len(summary["results"]), 3)
        self.assertFalse((self.out / "_all" / "predictions.jsonl").exists())

    def test_all_failed_records_failure(self):
        summary = self.run_with_failures({"beida", "xian", "wuhan"})
        self.assertEqual(summary["failed_datasets"], ["beida", "xian", "wuhan"])
        self.assertFalse((self.out / "_all" / "predictions.jsonl").exists())

    def test_failed_rerun_does_not_overwrite_existing_aggregate(self):
        aggregate = self.out / "_all" / "predictions.jsonl"
        aggregate.parent.mkdir()
        aggregate.write_text('{"previous": true}\n')
        original = aggregate.read_bytes()
        summary = self.run_with_failures({"xian"})
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(aggregate.read_bytes(), original)

    def test_f_base_propagates_failure_to_exit_report(self):
        with contextlib.redirect_stderr(io.StringIO()):
            args = experiment_f.parse_args(["--output-dir", str(self.out)])
        with patch.object(experiment_f, "run_all", side_effect=RuntimeError("1/3 datasets failed")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(experiment_f.stage_f6_base(args), 1)
        report = json.loads((self.out / "f6_base_report.json").read_text())
        self.assertEqual(report["returncode"], 1)
        self.assertIn("datasets failed", report["error"])


if __name__ == "__main__":
    unittest.main()
