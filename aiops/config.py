from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_WORKSPACE = Path(r"C:\Users\Cyber\Downloads\workspace")


@dataclass
class PipelineConfig:
    workspace: Path = DEFAULT_WORKSPACE
    output_dir: Path = Path("outputs")
    bin_minutes: int = 5
    seed: int = 42

    # metric anomaly models
    if_estimators: int = 200
    if_contamination: str | float = "auto"
    vae_hidden_dim: int = 64
    vae_latent_dim: int = 8
    vae_epochs: int = 40
    vae_batch_size: int = 512
    vae_beta: float = 0.0005
    vae_lr: float = 1e-3

    # pseudo labels
    pseudo_normal_quantile: float = 0.75
    pseudo_anomaly_quantile: float = 0.90

    # GNN
    # Whether the point pipeline runs the graph GNN at all.  Experiment C must
    # run WITHOUT it, while A/B/D/E run with it.
    #
    # NOTE: setting ``gnn_epochs = 0`` is NOT the same thing.  With zero epochs
    # the training loop never runs but the randomly initialised GCN is still
    # evaluated and its output becomes ``final_normal_score``, so the node
    # ranking would be driven by an untrained network rather than by the
    # unsupervised scores.  Use this flag to actually disable the GNN.
    use_gnn: bool = True
    gnn_hidden_dim: int = 64
    gnn_epochs: int = 120
    gnn_lr: float = 0.005
    gnn_dropout: float = 0.2
    gnn_reg_weight: float = 0.3
    gnn_max_nodes: int = 120_000

    # Second unsupervised model (experiments D and E).
    # D/E train IF+VAE, then a SECOND VAE restricted to what the first stage
    # calls normal, and only then run the GNN over the point graph.
    #   ""          off (A, B, C)
    #   "weighted"  D: weight = max(0, 1 - alpha * first_anomaly_score)
    #   "threshold" E: first_anomaly_score > threshold is excluded entirely
    second_stage_mode: str = ""
    second_stage_alpha: float = 1.0
    second_stage_threshold: float = 0.7
    second_stage_epochs: int = 20
    second_stage_hidden: int = 32
    second_stage_latent: int = 4
    second_stage_lr: float = 1e-3
    second_stage_beta: float = 5e-4
    second_stage_batch_size: int = 512

    # sampling / scale guards
    # Feature engineering reads this many netflow rows into the point table.
    max_netflow_rows: int = 250_000
    max_routing_labels: int = 500
    max_feature_columns: int = 256

    # topology evidence
    # How many netflow rows the TOPOLOGY builder scans.  0 means the whole file
    # (~35M rows per region) - the old code read only its first 50k rows, a time
    # prefix, which is why observed data-plane links were so sparse.  This is
    # deliberately separate from max_netflow_rows above: that one caps the
    # feature table, where pandas treats 0 as "no rows" rather than "all".
    netflow_topology_max_rows: int = 0

    # Links whose evidence confidence is below this are dropped from the point
    # graph.  0.0 keeps every evidenced link; raise it to keep only strong ones.
    topology_min_confidence: float = 0.0

    # LLM / MCP
    llm_backend: str = "none"  # none | ollama | openai_compatible | transformers
    llm_model: str = "qwen2.5:7b"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = ""
    llm_timeout_seconds: int = 120
    llm_max_new_tokens: int = 1024
    max_llm_tool_rounds: int = 8
    llm_tool_calling: bool = True

    # Experiment switch.  ``baseline`` preserves the original pipeline; the
    # other modes enable the Point -> Episode -> Incident -> RCA stages.
    experiment_mode: str = "baseline"  # baseline|episode|incident|llm_pseudo|hybrid|llm_distill|full
    prediction_level: str = "dataset"  # dataset|incident; final submission granularity

    # Temporal Episode
    episode_score_column: str = "combined_anomaly_score"
    episode_anomaly_quantile: float = 0.90
    episode_anomaly_threshold: float | None = None
    episode_max_gap_minutes: int = 5
    episode_min_duration_minutes: int = 5
    episode_min_abnormal_points: int = 2

    # Incident correlation / incidentization
    incident_max_time_gap_minutes: int = 30
    incident_topology_distance: int = 2
    incident_similarity_threshold: float = 0.45
    incident_merge_threshold: float = 0.65
    incident_node_overlap_weight: float = 0.35
    incident_temporal_weight: float = 0.25
    incident_topology_weight: float = 0.20
    incident_metric_weight: float = 0.10
    incident_event_weight: float = 0.10

    # Propagation
    propagation_max_lag_minutes: int = 30

    # RCA-oriented GNN
    rca_gnn_hidden_dim: int = 64
    rca_gnn_epochs: int = 120
    rca_gnn_lr: float = 0.005
    rca_gnn_dropout: float = 0.2
    lambda_rca: float = 1.0
    lambda_major: float = 0.20
    lambda_minor: float = 0.20

    # LLM teacher / pseudo labels
    llm_teacher_num_cases: int = 100
    llm_teacher_cache_dir: str = "_teacher"
    # Teacher and Task A used to call the backend one request at a time, which
    # left vLLM idle between calls.  Both stages are now concurrent.
    llm_teacher_workers: int = 8
    llm_task_a_workers: int = 8
    llm_task_a_max_pairs: int = 50
    llm_task_b_max_incidents: int = 5
    llm_teacher_max_tokens: int = 1024
    llm_pseudo_high_confidence: float = 0.85
    llm_pseudo_low_confidence: float = 0.60
    llm_anomaly_probability_threshold: float = 0.5

    def to_dict(self) -> dict:
        d = asdict(self)
        d["workspace"] = str(self.workspace)
        d["output_dir"] = str(self.output_dir)
        return d
