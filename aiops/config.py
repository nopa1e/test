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
    gnn_hidden_dim: int = 64
    gnn_epochs: int = 120
    gnn_lr: float = 0.005
    gnn_dropout: float = 0.2
    gnn_reg_weight: float = 0.3
    gnn_max_nodes: int = 120_000

    # sampling / scale guards
    max_netflow_rows: int = 250_000
    max_routing_labels: int = 500
    max_feature_columns: int = 256

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
