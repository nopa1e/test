"""The candidates stage must use its argument namespace, not an undefined config."""
import argparse
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiops.dataset import DatasetInfo
from aiops.evidence import f_stages

ROOT = Path(__file__).resolve().parents[1]


class CandidateStageTests(unittest.TestCase):
    def check_mode(self, enabled=None):
        output = ROOT / "outputs_experiment_code_optimization_20261011"
        output.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output) as tmp:
            ds = DatasetInfo("beida_test", "beida", Path(tmp))
            args = argparse.Namespace(output_dir=tmp, artifacts_dir=tmp, top_k=10)
            if enabled is not None:
                args.causal_graph = enabled
            bundle = {"metric_ev": {"incidents": {}}, "routing_ev": {}, "log_ev": {}, "quality_ev": {}}
            with patch.object(f_stages, "_matched", return_value=[ds]), \
                 patch.object(f_stages, "_load_incidents", return_value=[{"incident_id": "sample"}]), \
                 patch.object(f_stages, "_evidence_bundle", return_value=bundle), \
                 patch.object(f_stages, "build_temporal_evidence", return_value={}), \
                 patch.object(f_stages, "_load_adjacency", return_value={}), \
                 patch.object(f_stages, "build_candidates", return_value={"incidents": {}}) as build, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(f_stages.stage_candidates(args), 0)
                self.assertIs(build.call_args.kwargs["causal_graph_mode"], bool(enabled))

    def test_default_mode_does_not_reference_undefined_config(self):
        self.check_mode()

    def test_explicit_mode_is_forwarded(self):
        self.check_mode(True)


if __name__ == "__main__":
    unittest.main()
