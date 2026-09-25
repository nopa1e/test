from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .config import PipelineConfig
from .dataset import discover_datasets, network_element_id
from .utils import ensure_dir, get_logger, read_json

log = get_logger(__name__)


class DiagnosisToolbox:
    """Shared implementation of the MCP tools used by the LLM stage."""

    def __init__(self, output_dir: str | Path, workspace: str | Path | None = None):
        self.output_root = Path(output_dir)
        self.workspace = Path(workspace) if workspace else PipelineConfig().workspace
        self.datasets = discover_datasets(self.workspace)
        self.ds_by_name = {d.name: d for d in self.datasets}
        self._point_cache: dict[str, pd.DataFrame] = {}
        self._node_cache: dict[str, pd.DataFrame] = {}
        self._topo_cache: dict[str, dict[str, Any]] = {}
        self._evidence_cache: dict[str, dict[str, Any]] = {}
        self._episode_cache: dict[str, pd.DataFrame] = {}
        self._incident_cache: dict[str, list[dict[str, Any]]] = {}
        self._propagation_cache: dict[str, dict[str, Any]] = {}
        self._rca_cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # artifact loading
    # ------------------------------------------------------------------
    def _artifact_dir(self, dataset: str) -> Path:
        return self.output_root / dataset

    def list_datasets(self) -> list[dict[str, Any]]:
        out = []
        for ds in self.datasets:
            d = self._artifact_dir(ds.name)
            if d.exists():
                out.append({"dataset": ds.name, "region_code": ds.region_code, "output_dir": str(d)})
        return out

    def _resolve_dataset(self, network_element_id: str | None = None, dataset: str | None = None) -> str:
        if dataset:
            if dataset in self.ds_by_name:
                return dataset
            for name in self.ds_by_name:
                if dataset.lower() in name.lower():
                    return name
            raise ValueError(f"unknown dataset: {dataset}")
        if network_element_id:
            for ds in self.datasets:
                if str(network_element_id).startswith(ds.region_code + "-"):
                    return ds.name
        available = [d.name for d in self.datasets if self._artifact_dir(d.name).exists()]
        if available:
            return available[0]
        if self.datasets:
            return self.datasets[0].name
        raise ValueError("no datasets available")

    def _read_point_scores(self, dataset: str) -> pd.DataFrame:
        if dataset in self._point_cache:
            return self._point_cache[dataset]
        path = self._artifact_dir(dataset) / "point_scores.csv.gz"
        if not path.exists():
            self._point_cache[dataset] = pd.DataFrame()
            return self._point_cache[dataset]
        df = pd.read_csv(path, compression="gzip")
        self._point_cache[dataset] = df
        return df

    def _read_node_scores(self, dataset: str) -> pd.DataFrame:
        if dataset in self._node_cache:
            return self._node_cache[dataset]
        path = self._artifact_dir(dataset) / "node_scores.csv"
        if not path.exists():
            self._node_cache[dataset] = pd.DataFrame()
            return self._node_cache[dataset]
        df = pd.read_csv(path)
        self._node_cache[dataset] = df
        return df

    def _read_topology(self, dataset: str) -> dict[str, Any]:
        if dataset in self._topo_cache:
            return self._topo_cache[dataset]
        path = self._artifact_dir(dataset) / "topology.json"
        if not path.exists():
            self._topo_cache[dataset] = {"nodes": [], "edges": []}
            return self._topo_cache[dataset]
        data = read_json(path)
        self._topo_cache[dataset] = data
        return data

    def _read_evidence(self, dataset: str) -> dict[str, Any]:
        if dataset in self._evidence_cache:
            return self._evidence_cache[dataset]
        path = self._artifact_dir(dataset) / "evidence.json"
        if not path.exists():
            self._evidence_cache[dataset] = {}
            return self._evidence_cache[dataset]
        data = read_json(path)
        self._evidence_cache[dataset] = data
        return data

    def _read_episodes(self, dataset: str) -> pd.DataFrame:
        if dataset in self._episode_cache:
            return self._episode_cache[dataset]
        path = self._artifact_dir(dataset) / "episode_scores.csv"
        if not path.exists():
            self._episode_cache[dataset] = pd.DataFrame()
            return self._episode_cache[dataset]
        df = pd.read_csv(path)
        self._episode_cache[dataset] = df
        return df

    def _read_incidents(self, dataset: str) -> list[dict[str, Any]]:
        if dataset in self._incident_cache:
            return self._incident_cache[dataset]
        path = self._artifact_dir(dataset) / "incident_candidates.json"
        if not path.exists():
            self._incident_cache[dataset] = []
            return self._incident_cache[dataset]
        data = read_json(path)
        incidents = data.get("incidents", []) if isinstance(data, dict) else data
        if not isinstance(incidents, list):
            incidents = []
        self._incident_cache[dataset] = incidents
        return incidents

    def _read_propagation(self, dataset: str) -> dict[str, Any]:
        if dataset in self._propagation_cache:
            return self._propagation_cache[dataset]
        path = self._artifact_dir(dataset) / "propagation_graph.json"
        if not path.exists():
            self._propagation_cache[dataset] = {}
            return self._propagation_cache[dataset]
        data = read_json(path)
        self._propagation_cache[dataset] = data if isinstance(data, dict) else {}
        return self._propagation_cache[dataset]

    def _read_rca_features(self, dataset: str) -> pd.DataFrame:
        if dataset in self._rca_cache:
            return self._rca_cache[dataset]
        path = self._artifact_dir(dataset) / "rca_features.csv"
        if not path.exists():
            self._rca_cache[dataset] = pd.DataFrame()
            return self._rca_cache[dataset]
        df = pd.read_csv(path)
        self._rca_cache[dataset] = df
        return df

    # ------------------------------------------------------------------
    # MCP tools
    # ------------------------------------------------------------------
    def list_network_elements(self, dataset: str | None = None) -> dict[str, Any]:
        names = [dataset] if dataset else [d.name for d in self.datasets if self._artifact_dir(d.name).exists()]
        elements: list[dict[str, Any]] = []
        for name in names:
            topo = self._read_topology(name)
            for n in topo.get("nodes", []):
                elements.append({"dataset": name, "network_element_id": n})
        return {"network_elements": elements}

    def get_dependency(self, network_element_id: str, dataset: str | None = None) -> dict[str, Any]:
        ds_name = self._resolve_dataset(network_element_id, dataset)
        topo = self._read_topology(ds_name)
        incoming = [e for e in topo.get("edges", []) if e.get("target") == network_element_id]
        outgoing = [e for e in topo.get("edges", []) if e.get("source") == network_element_id]
        return {
            "dataset": ds_name,
            "network_element_id": network_element_id,
            "upstream": sorted({e["source"] for e in incoming}),
            "downstream": sorted({e["target"] for e in outgoing}),
            "incoming_edges": incoming,
            "outgoing_edges": outgoing,
        }

    def get_trace_upstream(
        self,
        network_element_id: str,
        depth: int = 3,
        dataset: str | None = None,
    ) -> dict[str, Any]:
        ds_name = self._resolve_dataset(network_element_id, dataset)
        topo = self._read_topology(ds_name)
        edges = topo.get("edges", [])
        parents: dict[str, list[str]] = {}
        for e in edges:
            parents.setdefault(e["target"], []).append(e["source"])
        node_scores = self._read_node_scores(ds_name)
        score_map = {}
        if not node_scores.empty and "network_element_id" in node_scores.columns:
            score_map = dict(zip(node_scores["network_element_id"].astype(str), node_scores["anomaly_score"]))
        visited: dict[str, dict[str, Any]] = {}
        frontier = [(network_element_id, 0)]
        while frontier:
            cur, d = frontier.pop(0)
            if cur in visited or d > int(depth):
                continue
            visited[cur] = {
                "network_element_id": cur,
                "depth": d,
                "anomaly_score": float(score_map.get(cur, 0.0)),
                "parents": parents.get(cur, []),
            }
            if d < int(depth):
                for p in parents.get(cur, []):
                    if p not in visited:
                        frontier.append((p, d + 1))
        return {
            "dataset": ds_name,
            "start": network_element_id,
            "upstream": list(visited.values()),
            "note": "Search follows reverse dependency edges (parent/upstream).",
        }

    def get_trace_downstream(
        self,
        network_element_id: str,
        depth: int = 3,
        dataset: str | None = None,
    ) -> dict[str, Any]:
        ds_name = self._resolve_dataset(network_element_id, dataset)
        topo = self._read_topology(ds_name)
        children: dict[str, list[str]] = {}
        for e in topo.get("edges", []):
            children.setdefault(e["source"], []).append(e["target"])
        visited: dict[str, dict[str, Any]] = {}
        frontier = [(network_element_id, 0)]
        while frontier:
            cur, d = frontier.pop(0)
            if cur in visited or d > int(depth):
                continue
            visited[cur] = {"network_element_id": cur, "depth": d, "children": children.get(cur, [])}
            if d < int(depth):
                for c in children.get(cur, []):
                    if c not in visited:
                        frontier.append((c, d + 1))
        return {"dataset": ds_name, "start": network_element_id, "downstream": list(visited.values())}


    def get_node_anomaly(
        self,
        network_element_id: str,
        dataset: str | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        ds_name = self._resolve_dataset(network_element_id, dataset)
        nodes = self._read_node_scores(ds_name)
        row: dict[str, Any] = {}
        if not nodes.empty and "network_element_id" in nodes.columns:
            hit = nodes[nodes["network_element_id"].astype(str) == str(network_element_id)]
            if not hit.empty:
                row = hit.iloc[0].to_dict()
        points = self._read_point_scores(ds_name)
        evidence = []
        if not points.empty and "network_element_id" in points.columns:
            sub = points[points["network_element_id"].astype(str) == str(network_element_id)].copy()
            sub = sub.sort_values("final_normal_score", ascending=True).head(int(top_k))
            for _, r in sub.iterrows():
                feats = []
                try:
                    feats = json.loads(str(r.get("top_feature_json", "[]")))
                except Exception:
                    pass
                evidence.append(
                    {
                        "timestamp": str(r.get("timestamp_bin", "")),
                        "normal_score": float(r.get("final_normal_score", 0.0)),
                        "top_features": feats,
                    }
                )
        return {
            "dataset": ds_name,
            "network_element_id": network_element_id,
            "node_score": row,
            "top_points": evidence,
        }


    def get_anomaly_points(self, limit: int = 20, dataset: str | None = None) -> dict[str, Any]:
        names = [dataset] if dataset else [d.name for d in self.datasets if self._artifact_dir(d.name).exists()]
        frames = []
        for name in names:
            df = self._read_point_scores(name)
            if not df.empty:
                d = df.copy()
                d["dataset"] = name
                frames.append(d)
        if not frames:
            return {"points": []}
        allp = pd.concat(frames, ignore_index=True, sort=False)
        allp = allp.sort_values("final_normal_score", ascending=True).head(int(limit))
        points = []
        for _, r in allp.iterrows():
            try:
                feats = json.loads(str(r.get("top_feature_json", "[]")))
            except Exception:
                feats = []
            points.append(
                {
                    "dataset": r.get("dataset"),
                    "point_id": r.get("point_id"),
                    "timestamp": str(r.get("timestamp_bin", "")),
                    "network_element_id": str(r.get("network_element_id", "")),
                    "normal_score": float(r.get("final_normal_score", 0.0)),
                    "point_rank": int(r.get("point_rank", 0)) if pd.notna(r.get("point_rank")) else None,
                    "top_features": feats,
                }
            )
        return {"points": points}

    def get_metric_evidence(
        self,
        network_element_id: str,
        dataset: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        result = self.get_node_anomaly(network_element_id, dataset=dataset, top_k=limit)
        counts: dict[str, int] = {}
        examples: dict[str, float] = {}
        for p in result.get("top_points", []):
            for f in p.get("top_features", []) or []:
                name = str(f.get("name", ""))
                counts[name] = counts.get(name, 0) + 1
                examples[name] = f.get("robust_z", 0.0)
        return {
            "dataset": result.get("dataset"),
            "network_element_id": network_element_id,
            "node_score": result.get("node_score"),
            "feature_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "feature_robust_z": examples,
            "top_points": result.get("top_points", []),
        }

    def get_frr_events(
        self,
        start_time: str,
        end_time: str,
        network_element_id: str | None = None,
        dataset: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        ds_name = self._resolve_dataset(network_element_id, dataset)
        ds = self.ds_by_name.get(ds_name)
        if ds is None or not ds.has("frr_syslog_events"):
            return {"dataset": ds_name, "events": []}
        df = pd.read_csv(ds.path("frr_syslog_events"), encoding="utf-8-sig", low_memory=False)
        time_col = "event_time" if "event_time" in df.columns else ("received_at" if "received_at" in df.columns else None)
        if time_col is None:
            return {"dataset": ds_name, "events": []}
        df["_ts"] = pd.to_datetime(df[time_col], errors="coerce", utc=True)
        start = pd.to_datetime(start_time, errors="coerce", utc=True)
        end = pd.to_datetime(end_time, errors="coerce", utc=True)
        if pd.notna(start):
            df = df[df["_ts"] >= start]
        if pd.notna(end):
            df = df[df["_ts"] <= end]
        if network_element_id and "hostname" in df.columns:
            target = str(network_element_id).split("-", 1)[-1].lower()
            df = df[df["hostname"].astype(str).str.lower().str.contains(target, regex=False)]
        cols = [c for c in ["event_time", "hostname", "severity", "severity_code", "program", "message"] if c in df.columns]
        out = []
        for _, r in df.head(int(limit)).iterrows():
            out.append({c: str(r.get(c, "")) for c in cols})
        return {"dataset": ds_name, "events": out}

    def get_temporal_neighbors(
        self,
        network_element_id: str,
        timestamp: str,
        window_minutes: int = 15,
        dataset: str | None = None,
    ) -> dict[str, Any]:
        ds_name = self._resolve_dataset(network_element_id, dataset)
        points = self._read_point_scores(ds_name)
        ts = pd.to_datetime(timestamp, errors="coerce", utc=True)
        nearby: list[dict[str, Any]] = []
        if not points.empty and "timestamp_bin" in points.columns and pd.notna(ts):
            df = points.copy()
            df["_ts"] = pd.to_datetime(df["timestamp_bin"], errors="coerce", utc=True)
            df = df[(df["_ts"] >= ts - pd.Timedelta(minutes=int(window_minutes))) & (df["_ts"] <= ts + pd.Timedelta(minutes=int(window_minutes)))]
            df = df.sort_values("_ts")
            for _, r in df.head(200).iterrows():
                nearby.append({
                    "timestamp": str(r.get("timestamp_bin", "")),
                    "network_element_id": str(r.get("network_element_id", "")),
                    "normal_score": float(r.get("final_normal_score", 0.0)) if pd.notna(r.get("final_normal_score", None)) else None,
                    "anomaly_score": float(r.get("combined_anomaly_score", 0.0)) if pd.notna(r.get("combined_anomaly_score", None)) else None,
                })
        topo = self._read_topology(ds_name)
        related = set()
        for e in topo.get("edges", []):
            if e.get("source") == network_element_id:
                related.add(e.get("target"))
            if e.get("target") == network_element_id:
                related.add(e.get("source"))
        return {
            "dataset": ds_name,
            "network_element_id": network_element_id,
            "timestamp": timestamp,
            "window_minutes": int(window_minutes),
            "nearby_points": nearby,
            "related_nodes": sorted(x for x in related if x),
        }

    def get_incident_candidates(self, dataset: str | None = None) -> dict[str, Any]:
        names = [dataset] if dataset else [d.name for d in self.datasets if self._artifact_dir(d.name).exists()]
        out: list[dict[str, Any]] = []
        for name in names:
            for inc in self._read_incidents(name):
                out.append({
                    "dataset": name,
                    "incident_id": inc.get("incident_id"),
                    "time_range": inc.get("time_range"),
                    "nodes": inc.get("nodes", []),
                    "episode_count": inc.get("episode_count"),
                    "node_count": inc.get("node_count"),
                })
        return {"incidents": out}

    def get_incident_timeline(self, incident_id: str, dataset: str | None = None) -> dict[str, Any]:
        names = [dataset] if dataset else [d.name for d in self.datasets if self._artifact_dir(d.name).exists()]
        for name in names:
            for inc in self._read_incidents(name):
                if str(inc.get("incident_id")) == str(incident_id):
                    return {
                        "dataset": name,
                        "incident_id": incident_id,
                        "time_range": inc.get("time_range"),
                        "nodes": inc.get("nodes", []),
                        "episodes": inc.get("episodes", []),
                        "timeline": inc.get("timeline", []),
                    }
        return {"error": "incident_not_found", "incident_id": incident_id}

    def compare_incidents(self, incident_a: str, incident_b: str, dataset: str | None = None) -> dict[str, Any]:
        import networkx as nx
        from .incident import compare_incidents as _compare

        names = [dataset] if dataset else [d.name for d in self.datasets if self._artifact_dir(d.name).exists()]
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        for name in names:
            for inc in self._read_incidents(name):
                key = str(inc.get("incident_id"))
                if key in {str(incident_a), str(incident_b)}:
                    found[key] = (name, inc)
            if len(found) == 2:
                break
        if len(found) < 2:
            return {"error": "incident_not_found", "found": sorted(found.keys())}
        ds_name = next(iter(found.values()))[0]
        topo = self._read_topology(ds_name)
        graph = nx.DiGraph()
        for e in topo.get("edges", []):
            graph.add_edge(e.get("source"), e.get("target"), edge_type=e.get("edge_type", "topology"))
        return {
            "dataset": ds_name,
            "incident_a": incident_a,
            "incident_b": incident_b,
            "relations": _compare(found[str(incident_a)][1], found[str(incident_b)][1], graph, self._read_point_scores(ds_name)),
        }

    def get_propagation_chain(self, incident_id: str, dataset: str | None = None) -> dict[str, Any]:
        names = [dataset] if dataset else [d.name for d in self.datasets if self._artifact_dir(d.name).exists()]
        for name in names:
            data = self._read_propagation(name)
            for prop in data.get("incidents", []) if isinstance(data, dict) else []:
                if str(prop.get("incident_id")) == str(incident_id):
                    return {
                        "dataset": name,
                        "incident_id": incident_id,
                        "chain": prop.get("chain", []),
                        "nodes": prop.get("nodes", []),
                        "edges": prop.get("edges", []),
                    }
        return {"error": "incident_not_found", "incident_id": incident_id}

    def get_fault_signature(self, incident_id: str, dataset: str | None = None) -> dict[str, Any]:
        names = [dataset] if dataset else [d.name for d in self.datasets if self._artifact_dir(d.name).exists()]
        for name in names:
            for inc in self._read_incidents(name):
                if str(inc.get("incident_id")) == str(incident_id):
                    signatures: set[str] = set()
                    for ep in inc.get("episodes", []) or []:
                        for sig in ep.get("metric_signature", []) or []:
                            signatures.add(str(sig))
                    return {
                        "dataset": name,
                        "incident_id": incident_id,
                        "nodes": inc.get("nodes", []),
                        "metric_signature": sorted(signatures),
                        "episode_count": inc.get("episode_count"),
                        "node_count": inc.get("node_count"),
                    }
        return {"error": "incident_not_found", "incident_id": incident_id}

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = dict(arguments or {})
        fn = getattr(self, str(name), None)
        if fn is None or not callable(fn):
            raise ValueError(f"unknown tool: {name}")
        return fn(**args)


def create_mcp_server(toolbox: DiagnosisToolbox):
    try:
        from fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("fastmcp is required for the MCP server: pip install fastmcp") from exc

    mcp = FastMCP("aiops-root-cause-tools", instructions="Query AIOps graph dependencies, trace upstream, node anomaly scores and syslog evidence.")

    @mcp.tool
    def list_datasets() -> list[dict[str, Any]]:
        """List datasets that have exported pipeline artifacts."""
        return toolbox.list_datasets()

    @mcp.tool
    def list_network_elements(dataset: str | None = None) -> dict[str, Any]:
        """List legal network_element_id values for one or all datasets."""
        return toolbox.list_network_elements(dataset=dataset)

    @mcp.tool
    def get_dependency(network_element_id: str, dataset: str | None = None) -> dict[str, Any]:
        """Return direct upstream and downstream dependencies of a network element."""
        return toolbox.get_dependency(network_element_id, dataset=dataset)

    @mcp.tool
    def get_trace_upstream(network_element_id: str, depth: int = 3, dataset: str | None = None) -> dict[str, Any]:
        """Walk reverse dependency/trace edges to find parent/upstream elements and their anomaly scores."""
        return toolbox.get_trace_upstream(network_element_id, depth=depth, dataset=dataset)

    @mcp.tool
    def get_trace_downstream(network_element_id: str, depth: int = 3, dataset: str | None = None) -> dict[str, Any]:
        """Walk forward dependency edges to find child/downstream elements."""
        return toolbox.get_trace_downstream(network_element_id, depth=depth, dataset=dataset)

    @mcp.tool
    def get_node_anomaly(network_element_id: str, dataset: str | None = None, top_k: int = 5) -> dict[str, Any]:
        """Return the normal-score ranking and top anomalous points of a network element."""
        return toolbox.get_node_anomaly(network_element_id, dataset=dataset, top_k=top_k)

    @mcp.tool
    def get_anomaly_points(limit: int = 20, dataset: str | None = None) -> dict[str, Any]:
        """Return the globally most anomalous points (lowest normal score first)."""
        return toolbox.get_anomaly_points(limit=limit, dataset=dataset)

    @mcp.tool
    def get_metric_evidence(network_element_id: str, dataset: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Return aggregated feature-level evidence for a network element."""
        return toolbox.get_metric_evidence(network_element_id, dataset=dataset, limit=limit)

    @mcp.tool
    def get_frr_events(
        start_time: str,
        end_time: str,
        network_element_id: str | None = None,
        dataset: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query FRR Syslog events in a time window and optional node filter."""
        return toolbox.get_frr_events(
            start_time,
            end_time,
            network_element_id=network_element_id,
            dataset=dataset,
            limit=limit,
        )

    @mcp.tool
    def get_temporal_neighbors(
        network_element_id: str,
        timestamp: str,
        window_minutes: int = 15,
        dataset: str | None = None,
    ) -> dict[str, Any]:
        """Return nearby points and topology-related nodes around a timestamp."""
        return toolbox.get_temporal_neighbors(
            network_element_id,
            timestamp,
            window_minutes=window_minutes,
            dataset=dataset,
        )

    @mcp.tool
    def get_incident_candidates(dataset: str | None = None) -> dict[str, Any]:
        """List incident candidates generated from temporal episodes."""
        return toolbox.get_incident_candidates(dataset=dataset)

    @mcp.tool
    def get_incident_timeline(incident_id: str, dataset: str | None = None) -> dict[str, Any]:
        """Return the full timeline of an incident candidate."""
        return toolbox.get_incident_timeline(incident_id, dataset=dataset)

    @mcp.tool
    def compare_incidents(incident_a: str, incident_b: str, dataset: str | None = None) -> dict[str, Any]:
        """Compare two incident candidates on temporal/topology/signature evidence."""
        return toolbox.compare_incidents(incident_a, incident_b, dataset=dataset)

    @mcp.tool
    def get_propagation_chain(incident_id: str, dataset: str | None = None) -> dict[str, Any]:
        """Return the candidate fault propagation chain for an incident."""
        return toolbox.get_propagation_chain(incident_id, dataset=dataset)

    @mcp.tool
    def get_fault_signature(incident_id: str, dataset: str | None = None) -> dict[str, Any]:
        """Return the metric/event signature of an incident."""
        return toolbox.get_fault_signature(incident_id, dataset=dataset)

    return mcp


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MCP server for AIOps root-cause inspection")
    p.add_argument("--output-dir", default=str(Path("outputs")))
    p.add_argument("--workspace", default=str(PipelineConfig().workspace))
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    toolbox = DiagnosisToolbox(args.output_dir, workspace=args.workspace)
    server = create_mcp_server(toolbox)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
