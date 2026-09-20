"""AIOps fault diagnosis pipeline.

A lightweight, reproducible implementation of the requested two-stage pipeline:

1. Metric anomaly detection with Isolation Forest and a VAE.
2. Pseudo-labelling normal points, trace/topology graph construction, and GNN
   normal-score learning.
3. Structured evidence generation for an LLM.
4. An MCP tool server that lets the LLM inspect dependencies and trace upstream.
"""

__version__ = "0.1.0"
