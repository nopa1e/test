"""Regression checks for NetFlow coverage, budgets, and the point-table join."""
from pathlib import Path
import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from aiops import features
from aiops.config import PipelineConfig
from aiops.dataset import DatasetInfo
import run_experiment_f_evidence_agent as experiment_f
import run_pipeline

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs_experiment_code_optimization_20261011"


class NetflowTests(unittest.TestCase):
    def setUp(self):
        OUTPUT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=OUTPUT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "netflow.csv"
        self.raw = pd.DataFrame({
            "minute_utc": pd.date_range("2026-10-01", periods=19, freq="min", tz="UTC").astype(str),
            "region_code": ["beida"] * 19,
            "node_key": ["cr1", "br2", "cr2"] * 6 + ["cr1"],
            "packets": list(range(1, 20)),
            "bytes": list(range(101, 120)),
            "flow_record_count": [1] * 19,
        })
        self.raw.to_csv(self.path, index=False)
        self.ds = DatasetInfo("beida_test", "beida", self.path.parent,
                              {"netflow_5tuple": self.path})
        self.cfg = PipelineConfig(bin_minutes=5, max_netflow_rows=0)

    def aggregate(self, budget=0, chunk_size=7):
        self.cfg.max_netflow_rows = budget
        with patch.object(features, "_NETFLOW_CHUNK_ROWS", chunk_size):
            return features._agg_netflow(self.ds, self.cfg)

    def expected(self, budget=0):
        raw = self.raw.iloc[:budget] if budget > 0 else self.raw
        out = features._netflow_frame_to_agg(raw, self.ds, self.cfg)
        return out.rename(columns={c: "netflow_" + c for c in features._NETFLOW_VALUE})

    def test_unlimited_matches_full_read_for_multiple_chunks(self):
        for size in [1, 7, 64]:
            with self.subTest(chunk_size=size):
                pd.testing.assert_frame_equal(self.aggregate(chunk_size=size), self.expected())

    def test_positive_budget_is_exact_including_partial_last_chunk(self):
        for budget in [1, 3, 7, 8, 18, 19, 30]:
            with self.subTest(budget=budget):
                pd.testing.assert_frame_equal(self.aggregate(budget), self.expected(budget))

    def test_negative_budget_remains_unlimited(self):
        pd.testing.assert_frame_equal(self.aggregate(-1), self.expected())

    def test_fallback_preserves_the_same_budget(self):
        original = features.read_table
        calls = []
        def flaky(ds, kind, **kwargs):
            calls.append(kwargs)
            if kwargs["usecols"] is not None:
                raise ValueError("injected column selection failure")
            return original(ds, kind, **kwargs)
        with patch.object(features, "read_table", side_effect=flaky):
            pd.testing.assert_frame_equal(self.aggregate(3), self.expected(3))
        self.assertEqual([c["nrows"] for c in calls], [3, 3])

    def test_reader_is_closed_after_success_and_iteration_failure(self):
        class Reader:
            def __init__(self, frame, fail):
                self.frame, self.fail, self.closed = frame, fail, False
            def __iter__(self):
                yield self.frame
                if self.fail:
                    raise ValueError("injected read failure")
            def close(self):
                self.closed = True
        failed = Reader(self.raw.iloc[:2], True)
        successful = Reader(self.raw, False)
        with patch.object(features, "read_table", side_effect=[failed, successful]):
            result = self.aggregate()
        self.assertTrue(failed.closed)
        self.assertTrue(successful.closed)
        pd.testing.assert_frame_equal(result, self.expected())

    def test_absent_netflow_remains_optional(self):
        self.ds.files.clear()
        self.assertTrue(self.aggregate().empty)

    def test_netflow_joins_by_dataset_node_and_time(self):
        node = pd.DataFrame({
            "timestamp": ["2026-10-01T00:00:00Z"] * 2,
            "region": ["ccf-aiops-北大"] * 2,
            "node": ["cr-1", "br-2"],
            "node_type": ["cr", "br"],
            "cpu_usage": [0.1, 0.2],
        })
        node_path = self.path.parent / "node.csv"
        node.to_csv(node_path, index=False)
        self.ds.files["node_metrics"] = node_path
        point, _ = features.build_point_table(self.ds, self.cfg)
        expected = {"beida-cr-1": 5, "beida-br-2": 7}
        self.assertEqual(dict(zip(point.network_element_id, point.netflow_packets)), expected)
        self.assertEqual(set(point.region), {"ccf-aiops-北大"})
        self.assertEqual(set(point.node_type), {"cr", "br"})
        self.assertEqual(len(point), 2)

    def test_f_base_default_uses_shared_config_and_flow_keeps_legacy_budget(self):
        with contextlib.redirect_stderr(io.StringIO()):
            base = experiment_f.parse_args([])
            flow = experiment_f.parse_args(["--stage", "flow"])
            explicit = experiment_f.parse_args(["--max-netflow-rows", "11"])
        self.assertEqual(experiment_f.build_cfg(base).max_netflow_rows,
                         PipelineConfig().max_netflow_rows)
        self.assertEqual(flow.max_netflow_rows, 250_000)
        self.assertEqual(experiment_f.build_cfg(explicit).max_netflow_rows, 11)

    def test_main_cli_default_and_override(self):
        for extra, budget in [([], 0), (["--max-netflow-rows", "13"], 13)]:
            with self.subTest(extra=extra), patch("sys.argv", ["run_pipeline.py"] + extra), \
                 patch.object(run_pipeline, "run_all", return_value={}) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                run_pipeline.main()
                self.assertEqual(run.call_args.args[1].max_netflow_rows, budget)


if __name__ == "__main__":
    unittest.main()
