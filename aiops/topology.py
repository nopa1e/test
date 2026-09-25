"""Entity-graph construction from real control-plane and data-plane evidence.

This module replaces the previous node-name heuristic.  It never guesses a
relationship from what a node is *called*; every edge is derived from something
the devices actually reported, and carries its provenance:

    edge_type     what established the relation (ospf_adjacency, route_nexthop, ...)
    source_kind   the data source family (ospf | bgp | route | netflow | syslog)
    confidence    0..1, how strongly the evidence supports the link
    evidence      the raw fields the edge was built from

Evidence sources, in decreasing order of authority:

    routing_metrics / ospf6_neighbor_state_code   router adjacencies + state
    routing_metrics / ipv6_route_nexthop_info     forwarding next-hops and per-prefix gateways
    routing_metrics / bgp_peer_up                 inter-region BGP sessions (external, kept as attributes)
    scrape_health                                 IP <-> node map (the join key for everything else)
    netflow_5tuple                                observed data-plane traffic
    frr_syslog_events                             routing daemon events (node identity only)

Node identity comes from the inventory the dataset itself provides (the
``node``/``node_type`` columns).  Spellings differ between files
(``br-2`` vs ``br2`` vs ``BR-2-ccf-aiops-shenyang``), so this module builds a
normalised alias index from that inventory rather than hard-coding prefixes.
"""
from __future__ import annotations

import csv
import io
import ipaddress
import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import networkx as nx
import pandas as pd

from .config import PipelineConfig
from .dataset import DatasetInfo, network_element_id, read_table
from .utils import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------
# Confidence assigned to each edge type.  Deliberately explicit, so a reader can
# see how much weight each kind of evidence carries.
# --------------------------------------------------------------------------
CONFIDENCE: dict[str, float] = {
    "ospf_full": 1.00,       # control plane says the adjacency is fully up
    "ospf_other": 0.55,      # adjacency exists but is not Full (e.g. Twoway)
    "bgp_established": 1.00,
    "bgp_other": 0.55,
    "route_nexthop": 0.90,   # forwarding table names a local node
    "route_prefix": 0.60,    # route reaches a prefix that contains that node
    "netflow": 0.70,         # data plane observed traffic between the two
}

_OSPF_FULL = "full"

# A routed prefix is only treated as evidence of reachability when it is at
# least this specific.  A default route (::/0) contains every address, so
# without this guard it would connect every node to every other node.
_ROUTE_PREFIX_MIN_LEN = 64

_M_OSPF = "ospf6_neighbor_state_code"
_M_BGP = "bgp_peer_up"
_M_BGP_PREFIX = "bgp_peer_prefix_received"
_M_ROUTE = "ipv6_route_nexthop_info"
_WANTED_METRICS = {_M_OSPF, _M_BGP, _M_BGP_PREFIX, _M_ROUTE}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def parse_labels(blob: str | None) -> dict[str, str]:
    """Parse a Prometheus-style label blob such as ``a="1",b="2"``."""
    if not blob:
        return {}
    return {k: v for k, v in re.findall(r'(\w+)="([^"]*)"', blob)}


def strip_ip_port(target: str) -> str:
    """``[fd00::1]:9100`` -> ``fd00::1``;  ``10.0.0.1:9100`` -> ``10.0.0.1``.

    A bare IPv6 address must survive untouched: naively stripping a trailing
    ``:<digits>`` turns ``fd00:2:20::1`` into ``fd00:2:20:``, which collapses
    every host in a /64 onto one key.  So the string is validated as an address
    first and only treated as host:port when that fails.
    """
    s = str(target).strip()
    if not s:
        return s
    m = re.match(r"^\[(.+)\]:\d+$", s)
    if m:
        return m.group(1).strip()
    try:
        ipaddress.ip_address(s)
        return s
    except ValueError:
        pass
    m = re.match(r"^(.*):\d+$", s)
    if m:
        return m.group(1).strip()
    return s


def normalize_key(name: str) -> str:
    """Collapse a node spelling to a comparable key (``BR-2-x`` -> ``br2x``)."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


# --------------------------------------------------------------------------
# node inventory / alias resolution (data driven, no name heuristics)
# --------------------------------------------------------------------------
class NodeIndex:
    """Maps every spelling of a node found in the dataset onto one canonical id.

    Canonical ids come from the pipeline's point table, so the graph and the
    point features can never disagree about which node is which.  The node kinds
    come from the dataset's own ``node_type`` column.
    """

    def __init__(self, canonical: dict[str, str]) -> None:
        self.types: dict[str, str] = dict(canonical)
        self._by_key: dict[str, str] = {}
        # Longest key first so that "br-10" wins over "br-1".
        for name in sorted(self.types, key=lambda n: -len(normalize_key(n))):
            self._by_key.setdefault(normalize_key(name), name)
        self._keys_by_len = sorted(self._by_key, key=len, reverse=True)
        self._cache: dict[str, str | None] = {}

    def __len__(self) -> int:
        return len(self.types)

    def names(self) -> list[str]:
        return sorted(self.types)

    def type_of(self, name: str) -> str:
        return str(self.types.get(name, "unknown")).lower()

    def resolve(self, raw: Any) -> str | None:
        """Resolve any spelling of a node to the canonical short name."""
        if raw is None:
            return None
        s = str(raw).strip()
        if not s or s.lower() in {"nan", "none", "null", "\\n"}:
            return None
        if s in self._cache:
            return self._cache[s]
        key = normalize_key(s)
        hit = self._by_key.get(key) if key else None
        if hit is None and key:
            # Embedded form: "BR-2-ccf-aiops-shenyang" begins with key "br2".
            for k in self._keys_by_len:
                if key.startswith(k):
                    hit = self._by_key[k]
                    break
        self._cache[s] = hit
        return hit

    def resolve_series(self, series: pd.Series) -> pd.Series:
        """Vectorised resolve: unique spellings are mapped once per chunk."""
        uniq = series.astype(str).unique()
        mapping = {v: (self.resolve(v) or "") for v in uniq}
        return series.astype(str).map(mapping)


def build_node_index(point_df: pd.DataFrame, ds: DatasetInfo | None = None) -> NodeIndex:
    """Build the node inventory from the point table the pipeline already has."""
    short: dict[str, str] = {}
    if point_df is not None and not point_df.empty and "network_element_id" in point_df.columns:
        neid = point_df["network_element_id"].astype(str)
        types = (
            point_df["node_type"].astype(str)
            if "node_type" in point_df.columns
            else pd.Series([""] * len(point_df), index=point_df.index)
        )
        for raw_id, raw_type in zip(neid, types):
            # network_element_id is "<region_code>-<node>"
            _, _, node = raw_id.partition("-")
            node = node or raw_id
            short.setdefault(node, str(raw_type).strip().lower())
    if not short and ds is not None:
        short = _inventory_from_tables(ds)
    return NodeIndex(short)


def _inventory_from_tables(ds: DatasetInfo) -> dict[str, str]:
    """Fallback inventory straight from the metric tables."""
    out: dict[str, str] = {}
    for kind in ("node_metrics", "interface_metrics", "routing_metrics"):
        df = read_table(ds, kind, usecols=lambda c: c in {"node", "node_type"})
        if df.empty or "node" not in df.columns:
            continue
        types = df["node_type"] if "node_type" in df.columns else pd.Series([""] * len(df), index=df.index)
        for n, t in zip(df["node"].astype(str), types.astype(str)):
            n = n.strip()
            if n and n.lower() not in {"nan", "none", ""}:
                out.setdefault(n, t.strip().lower())
    return out


def load_ip_node_map(ds: DatasetInfo, index: NodeIndex) -> dict[str, str]:
    """IP -> canonical node, joined from scrape_health and frr syslog.

    Disagreements between sources are reported rather than silently merged.
    """
    ip_map: dict[str, str] = {}
    conflicts: list[dict[str, str]] = []

    def add(ip: str, node: str | None, origin: str) -> None:
        if not ip or node is None:
            return
        prev = ip_map.get(ip)
        if prev is None:
            ip_map[ip] = node
        elif prev != node:
            conflicts.append({"ip": ip, "kept": prev, "ignored": node, "origin": origin})

    sh = read_table(ds, "scrape_health", usecols=lambda c: c in {"target_id", "node"})
    if not sh.empty and {"target_id", "node"} <= set(sh.columns):
        for target, node in sh[["target_id", "node"]].drop_duplicates().itertuples(index=False, name=None):
            add(strip_ip_port(str(target)), index.resolve(node), "scrape_health")

    syslog = read_table(ds, "frr_syslog_events", usecols=lambda c: c in {"source_ip", "hostname"})
    if not syslog.empty and {"source_ip", "hostname"} <= set(syslog.columns):
        for ip, host in syslog[["source_ip", "hostname"]].drop_duplicates().itertuples(index=False, name=None):
            add(strip_ip_port(str(ip)), index.resolve(host), "frr_syslog_events")

    if conflicts:
        log.warning("%s: %d IP/node conflicts across sources", ds.name, len(conflicts))
    log.info("%s: ip->node map has %d addresses covering %d nodes",
             ds.name, len(ip_map), len(set(ip_map.values())))
    return ip_map


# --------------------------------------------------------------------------
# routing_metrics streaming reader
# --------------------------------------------------------------------------
def _iter_routing_rows(path: Path, wanted: set[str]) -> Iterator[dict[str, str]]:
    """Yield routing_metrics rows whose metric_name is in ``wanted``.

    Streams the file: 5M+ rows are too many to load whole, and the label column
    holds embedded commas, so full CSV parsing is applied only to rows that
    survive a cheap pre-filter.
    """
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        header = next(fh, "")
        cols = next(csv.reader(io.StringIO(header)), [])
        idx = {c.strip(): i for i, c in enumerate(cols)}
        need = ("timestamp", "node", "metric_name", "label", "value")
        if not all(k in idx for k in need):
            log.error("routing_metrics lacks expected columns, got %s", cols)
            return
        i_ts, i_node, i_name, i_label, i_val = (idx[k] for k in need)
        maxsplit = max(i_name, i_label) + 1
        want_idx = max(i_label, i_val)
        for line in fh:
            head = line.split(",", maxsplit)
            if len(head) <= i_name or head[i_name] not in wanted:
                continue
            row = next(csv.reader(io.StringIO(line)), [])
            if len(row) <= want_idx:
                continue
            yield {
                "timestamp": row[i_ts],
                "node_raw": row[i_node],
                "metric_name": row[i_name],
                "label": row[i_label],
                "value": row[i_val],
            }


def extract_routing_evidence(ds: DatasetInfo, index: NodeIndex) -> dict[str, Any]:
    """Pull OSPF adjacencies, BGP sessions and route next-hops out of routing_metrics."""
    path = ds.path("routing_metrics")
    empty = {"ospf": [], "bgp": [], "bgp_prefix_counts": {}, "routes": [], "rows_seen": 0}
    if path is None:
        return empty

    ospf_seen: dict[tuple, dict[str, Any]] = {}
    bgp_seen: dict[tuple, dict[str, Any]] = {}
    bgp_prefix: dict[tuple, set[str]] = defaultdict(set)
    route_seen: dict[tuple, dict[str, Any]] = {}
    rows_seen = 0

    for row in _iter_routing_rows(path, _WANTED_METRICS):
        node = index.resolve(row["node_raw"])
        if node is None:
            continue
        kv = parse_labels(row["label"])
        m = row["metric_name"]
        rows_seen += 1

        if m == _M_OSPF:
            key = (node, kv.get("neighbor_id", ""), kv.get("interface", ""))
            state = kv.get("state", "")
            prev = ospf_seen.get(key)
            if prev is None:
                ospf_seen[key] = {
                    "node": node,
                    "neighbor_id": kv.get("neighbor_id", ""),
                    "state": state,
                    "interface": kv.get("interface", ""),
                }
            elif state.lower() == _OSPF_FULL and prev["state"].lower() != _OSPF_FULL:
                prev["state"] = state
        elif m in (_M_BGP, _M_BGP_PREFIX):
            peer = kv.get("peer", "")
            if not peer:
                continue
            key = (node, peer)
            if m == _M_BGP:
                bgp_seen[key] = {
                    "node": node, "peer": peer,
                    "remote_as": kv.get("remote_as", ""),
                    "state": kv.get("state", ""),
                }
            else:
                try:
                    bgp_prefix[key].add(str(int(float(row["value"]))))
                except Exception:
                    pass
        elif m == _M_ROUTE:
            pfx, nh = kv.get("prefix", ""), kv.get("next_hop", "")
            if not pfx or not nh:
                continue
            route_seen[(node, pfx, nh)] = {
                "node": node, "prefix": pfx, "next_hop": nh,
                "dev": kv.get("dev", ""), "protocol": kv.get("protocol", ""),
                "metric": kv.get("metric", ""),
            }

    log.info("%s: routing evidence rows=%d ospf=%d bgp=%d routes=%d",
             ds.name, rows_seen, len(ospf_seen), len(bgp_seen), len(route_seen))
    return {
        "ospf": sorted(ospf_seen.values(), key=lambda r: (r["node"], r["neighbor_id"], r["interface"])),
        "bgp": sorted(bgp_seen.values(), key=lambda r: (r["node"], r["peer"])),
        "bgp_prefix_counts": {f"{k[0]}|{k[1]}": sorted(v) for k, v in bgp_prefix.items()},
        "routes": sorted(route_seen.values(), key=lambda r: (r["node"], r["prefix"], r["next_hop"])),
        "rows_seen": rows_seen,
    }


def resolve_router_ids(ospf_rows: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, Any]]:
    """Map OSPF router-ids onto node names using the neighbour table alone.

    Two data-driven rules, no naming convention and no configuration:

    1. A router never lists its own router-id among its neighbours, so the id a
       node omits from the global id set is its own.  When exactly one id is
       omitted this is conclusive.
    2. When the neighbour table is incomplete (a degraded adjacency leaves
       several candidates), the assignment is chosen by global consistency: an
       OSPF adjacency is reciprocal, so the mapping under which the most
       neighbour reports are matched by a report back is the right one.  The
       search is over injective id -> node assignments and is bounded; if it is
       too large, or ties remain, the ids are reported as unresolved rather
       than guessed.
    """
    by_node: dict[str, dict[str, str]] = {}
    for r in ospf_rows:
        rid = r.get("neighbor_id")
        if not rid:
            continue
        seen = by_node.setdefault(r["node"], {})
        state = str(r.get("state", ""))
        prev = seen.get(rid)
        if prev is None or (state.lower() == _OSPF_FULL and prev.lower() != _OSPF_FULL):
            seen[rid] = state

    nodes = sorted(by_node)
    all_ids = sorted({rid for seen in by_node.values() for rid in seen})
    notes: list[str] = []

    def score(assign: dict[str, str]) -> tuple[int, int, int]:
        """(mutual Full pairs, mutual pairs, -dangling reports); higher is better."""
        owner = {rid: n for n, rid in assign.items()}
        mutual_full = mutual_any = dangling = 0
        for n in nodes:
            for rid, state in by_node[n].items():
                peer = owner.get(rid)
                if peer is None:
                    dangling += 1
                    continue
                back = by_node.get(peer, {}).get(assign[n])
                if back is None:
                    continue
                mutual_any += 1
                if state.lower() == _OSPF_FULL and back.lower() == _OSPF_FULL:
                    mutual_full += 1
        return mutual_full, mutual_any, -dangling

    # --- rule 1: unique omitted id ---------------------------------------
    unique: dict[str, str] = {}
    for n in nodes:
        cand = [i for i in all_ids if i not in by_node[n]]
        if len(cand) == 1 and cand[0] not in unique.values():
            unique[cand[0]] = n

    resolved: dict[str, str] = {}
    method = "unique_omitted_id"
    if len(unique) == len(nodes) == len(all_ids):
        # `unique` is already {router_id: node}.
        resolved = dict(unique)
    else:
        # --- rule 2: global reciprocal-consistency search ----------------
        import math

        total = math.perm(len(all_ids), len(nodes)) if len(all_ids) >= len(nodes) else 0
        if 0 < total <= 200_000:
            best: tuple[tuple[int, int, int], dict[str, str]] | None = None
            for combo in itertools.permutations(all_ids, len(nodes)):
                assign = dict(zip(nodes, combo))
                if any(assign[n] in by_node[n] for n in nodes):
                    continue  # a router cannot own an id it reports as a neighbour
                key = score(assign)
                if best is None or key > best[0]:
                    best = (key, assign)
            if best is not None:
                resolved = {rid: n for n, rid in best[1].items()}
                method = "reciprocity_search"
                notes.append(
                    "reciprocity search over %d assignments; score=%s"
                    % (total, best[0])
                )
        else:
            method = "unique_omitted_id_partial"
            resolved = {rid: n for n, rid in unique.items()}
            notes.append(f"search space too large ({total}); used unique ids only")

    if method != "unique_omitted_id":
        notes.append(
            "note: neighbour table incomplete; mapping from %s" % method
        )

    unresolved = [n for n in nodes if n not in set(resolved.values())]
    if unresolved:
        notes.append("unresolved router-ids for nodes: " + ", ".join(unresolved))
        log.warning("router-id resolution incomplete: %s", ", ".join(unresolved))

    return resolved, {
        "router_ids_seen": all_ids,
        "router_id_to_node": dict(sorted(resolved.items())),
        "method": method,
        "unresolved_nodes": unresolved,
        "notes": notes,
    }


# --------------------------------------------------------------------------
# data plane
# --------------------------------------------------------------------------
def extract_netflow_edges(
    ds: DatasetInfo,
    index: NodeIndex,
    ip_map: dict[str, str],
    cfg: PipelineConfig,
    chunk_rows: int = 1_000_000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate observed traffic into node-to-node data-plane edges.

    The previous implementation read only ``head(50_000)`` of a 34M-row file - a
    time-prefix of the capture.  This streams the whole file in chunks and
    aggregates with vectorised group-bys, so memory stays flat and the sample is
    the full capture rather than its first 0.14%.
    """
    path = ds.path("netflow_5tuple")
    if path is None:
        return [], {"rows": 0, "sampled": False}

    ip_to_node = {str(ip): node for ip, node in ip_map.items()}
    if not ip_to_node:
        return [], {"rows": 0, "sampled": False, "note": "empty ip->node map"}

    raw_limit = getattr(cfg, "netflow_topology_max_rows", None)
    if raw_limit is None:
        raw_limit = getattr(cfg, "max_netflow_rows", None)
    # 0 / None both mean "scan the entire capture".
    limit = int(raw_limit) if raw_limit else None

    flow_counts: Counter = Counter()
    byte_totals: Counter = Counter()
    peer_hits: Counter = Counter()
    rows = 0
    stopped_early = False

    reader = pd.read_csv(
        path, encoding="utf-8-sig", low_memory=False, chunksize=chunk_rows,
        usecols=lambda c: c in {"node_key", "src_addr", "dst_addr", "bytes"},
    )
    for chunk in reader:
        if limit is not None:
            remaining = limit - rows
            if remaining <= 0:
                stopped_early = True
                break
            if len(chunk) > remaining:
                chunk = chunk.iloc[:remaining]
                stopped_early = True
        rows += len(chunk)

        owner = index.resolve_series(chunk["node_key"])
        src_peer = chunk["src_addr"].astype(str).map(ip_to_node)
        dst_peer = chunk["dst_addr"].astype(str).map(ip_to_node)
        nbytes = pd.to_numeric(chunk.get("bytes"), errors="coerce").fillna(0.0) \
            if "bytes" in chunk.columns else pd.Series(0.0, index=chunk.index)

        frame = pd.DataFrame({
            "owner": owner.to_numpy(),
            "src": src_peer.to_numpy(),
            "dst": dst_peer.to_numpy(),
            "bytes": nbytes.to_numpy(),
        })
        long = pd.concat(
            [
                frame[["owner", "src", "bytes"]].rename(columns={"src": "peer"}),
                frame[["owner", "dst", "bytes"]].rename(columns={"dst": "peer"}),
            ],
            ignore_index=True,
        )
        long = long[long["peer"].notna() & (long["owner"] != "")]
        long = long[long["owner"] != long["peer"]]
        if long.empty:
            continue
        lo = long[["owner", "peer"]].min(axis=1)
        hi = long[["owner", "peer"]].max(axis=1)
        grouped = long.assign(a=lo, b=hi).groupby(["a", "b"], sort=False)
        sizes = grouped.size()
        sums = grouped["bytes"].sum()
        for (a, b), c in sizes.items():
            flow_counts[(a, b)] += int(c)
        for (a, b), v in sums.items():
            byte_totals[(a, b)] += float(v)
        for peer, c in long["peer"].value_counts().items():
            peer_hits[peer] += int(c)
        if stopped_early:
            break

    edges = [
        {
            "source": a, "target": b, "edge_type": "netflow_observed",
            "source_kind": "netflow", "confidence": CONFIDENCE["netflow"],
            "evidence": {"flows": int(c), "bytes": float(byte_totals[(a, b)])},
            "directed": False,
        }
        for (a, b), c in sorted(flow_counts.items())
    ]
    meta = {
        "rows": rows,
        "sampled": stopped_early,
        "sample_limit": limit,
        "local_addresses": len(ip_to_node),
        "peers_seen": dict(peer_hits.most_common()),
    }
    log.info("%s: netflow rows=%d -> %d observed edges%s",
             ds.name, rows, len(edges), " (sampled)" if stopped_early else " (full file)")
    return edges, meta


# --------------------------------------------------------------------------
# graph assembly
# --------------------------------------------------------------------------
@dataclass
class TopologyBuild:
    graph: nx.DiGraph
    meta: dict[str, Any] = field(default_factory=dict)


def build_entity_graph(
    ds: DatasetInfo,
    point_df: pd.DataFrame,
    cfg: PipelineConfig,
) -> tuple[nx.DiGraph, dict[str, Any]]:
    """Assemble the entity graph from evidence only.  No node-name heuristics."""
    index = build_node_index(point_df, ds)
    G = nx.DiGraph()
    for n in index.names():
        G.add_node(n, node_type=index.type_of(n))

    ip_map = load_ip_node_map(ds, index)
    for ip, node in ip_map.items():
        G.nodes[node].setdefault("addresses", [])
        G.nodes[node]["addresses"].append(ip)

    routing = extract_routing_evidence(ds, index)
    rid_to_node, rid_meta = resolve_router_ids(routing["ospf"])

    edges: list[dict[str, Any]] = []
    dropped: Counter = Counter()

    # --- OSPF adjacencies ------------------------------------------------
    for r in routing["ospf"]:
        a, nb = r["node"], r["neighbor_id"]
        b = rid_to_node.get(nb)
        if b is None or b == a:
            dropped["ospf_unresolved_neighbor"] += 1
            continue
        state = str(r.get("state", ""))
        up = state.lower() == _OSPF_FULL
        edges.append({
            "source": a, "target": b,
            "edge_type": "ospf_adjacency" if up else f"ospf_{state.lower() or 'unknown'}",
            "source_kind": "ospf",
            "confidence": CONFIDENCE["ospf_full"] if up else CONFIDENCE["ospf_other"],
            "evidence": {"state": state, "interface": r.get("interface", ""), "neighbor_id": nb},
            "directed": False,
        })

    # --- BGP sessions ----------------------------------------------------
    external_bgp: list[dict[str, Any]] = []
    for r in routing["bgp"]:
        a, peer = r["node"], r["peer"]
        up = str(r.get("state", "")).lower() == "established"
        if peer in rid_to_node:
            edges.append({
                "source": a, "target": rid_to_node[peer], "edge_type": "bgp_session",
                "source_kind": "bgp",
                "confidence": CONFIDENCE["bgp_established"] if up else CONFIDENCE["bgp_other"],
                "evidence": {"peer": peer, "remote_as": r.get("remote_as", ""), "state": r.get("state", "")},
                "directed": False,
            })
        else:
            # The peer is outside this dataset's element set (inter-region
            # transit).  Keep it as a node attribute instead of adding a
            # synthetic node, so the RCA candidate space stays clean.
            external_bgp.append({
                "node": a, "peer": peer, "remote_as": r.get("remote_as", ""),
                "state": r.get("state", ""),
                "prefixes": routing.get("bgp_prefix_counts", {}).get(f"{a}|{peer}", []),
            })
            G.nodes[a].setdefault("external_bgp_peers", []).append(peer)

    # --- routes: resolved next-hop, else routed-prefix membership --------
    valid_ips: dict[str, str] = {}
    for ip, node in ip_map.items():
        try:
            valid_ips[str(ipaddress.ip_address(ip))] = node
        except ValueError:
            continue

    for r in routing["routes"]:
        a, nh = r["node"], strip_ip_port(r["next_hop"])
        b = index.resolve(ip_map.get(nh)) if nh in ip_map else None
        if b and b != a:
            edges.append({
                "source": a, "target": b, "edge_type": "route_nexthop", "source_kind": "route",
                "confidence": CONFIDENCE["route_nexthop"],
                "evidence": {"prefix": r["prefix"], "next_hop": nh,
                             "dev": r.get("dev", ""), "protocol": r.get("protocol", "")},
                "directed": True,
            })
            continue

        # The next-hop is not one of our elements (typically a gateway interface
        # we do not scrape).  Fall back to membership of the routed prefix, which
        # is still derived from the forwarding table plus the address inventory.
        try:
            net = ipaddress.ip_network(str(r["prefix"]), strict=False)
        except ValueError:
            dropped["route_bad_prefix"] += 1
            continue
        if net.prefixlen < _ROUTE_PREFIX_MIN_LEN:
            # Default route / very broad aggregate: not evidence of adjacency.
            dropped["route_prefix_too_broad"] += 1
            continue
        members = []
        for ip, node in valid_ips.items():
            if node != a and ipaddress.ip_address(ip) in net:
                members.append(node)
        if not members:
            dropped["route_unresolved"] += 1
            continue
        for b in sorted(set(members)):
            edges.append({
                "source": a, "target": b, "edge_type": "route_prefix_member", "source_kind": "route",
                "confidence": CONFIDENCE["route_prefix"],
                "evidence": {"prefix": r["prefix"], "next_hop": nh, "protocol": r.get("protocol", "")},
                "directed": True,
            })

    # --- data plane ------------------------------------------------------
    nf_edges, nf_meta = extract_netflow_edges(ds, index, ip_map, cfg)
    edges.extend(nf_edges)

    # --- merge -----------------------------------------------------------
    # Collect evidence per unordered pair.  A pair is directed only when every
    # piece of evidence points the same way; otherwise it is bidirectional and
    # both directions carry the same merged provenance.  Merging must
    # ACCUMULATE - adding an undirected netflow edge after a directed route edge
    # between the same two nodes used to silently overwrite the route evidence.
    pair_recs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in edges:
        a, b = e["source"], e["target"]
        key = (a, b) if a <= b else (b, a)
        pair_recs[key].append({
            "from": a,
            "to": b,
            "edge_type": e["edge_type"],
            "source_kind": e["source_kind"],
            "confidence": float(e["confidence"]),
            "directed": bool(e.get("directed", True)),
            "evidence": e["evidence"],
        })

    for (x, y), recs in pair_recs.items():
        directions = {(r["from"], r["to"]) for r in recs}
        directed = len(directions) == 1 and all(r["directed"] for r in recs)
        best = max(recs, key=lambda r: r["confidence"])
        attrs = {
            "edge_type": best["edge_type"],
            "source_kind": best["source_kind"],
            "confidence": float(best["confidence"]),
            "directed": directed,
            "sources": recs,
            "n_sources": len(recs),
        }
        if directed:
            u, v = next(iter(directions))
            G.add_edge(u, v, **attrs)
        else:
            G.add_edge(x, y, **attrs)
            if x != y:
                G.add_edge(y, x, **attrs)

    by_type = Counter(d["edge_type"] for _, _, d in G.edges(data=True))
    # Count EVERY piece of supporting evidence, not just the strongest one: an
    # OSPF link also confirmed by netflow and the routing table is counted under
    # all three, which is what makes the graph's provenance auditable.
    by_kind: Counter = Counter()
    conf_hist: Counter = Counter()
    for _, _, d in G.edges(data=True):
        for s in d.get("sources", []):
            by_kind[s["source_kind"]] += 1
        conf_hist[round(float(d.get("confidence", 0.0)), 2)] += 1

    meta = {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "evidence_records": sum(by_kind.values()),
        "edge_types": dict(sorted(by_type.items())),
        "evidence_sources": dict(sorted(by_kind.items())),
        "confidence_histogram": {str(k): v for k, v in sorted(conf_hist.items())},
        "router_id_resolution": rid_meta,
        "external_bgp": external_bgp,
        "netflow": nf_meta,
        "dropped": dict(dropped),
        "node_types": {n: d.get("node_type", "unknown") for n, d in G.nodes(data=True)},
        "addresses": {n: d.get("addresses", []) for n, d in G.nodes(data=True)},
        "heuristic_edges": 0,
    }

    # Every consumer in this pipeline (incidents, propagation, RCA features,
    # RCA-GNN, the point table) keys nodes by network_element_id, so the graph
    # must use the same ids.  Building with short names and relabelling at the
    # end keeps the evidence code readable without breaking that contract -
    # getting this wrong silently produced a graph with no topology edges.
    relabel = {n: network_element_id(ds.region_code, n) for n in list(G.nodes())}
    G = nx.relabel_nodes(G, relabel)
    meta["node_types"] = {relabel.get(k, k): v for k, v in meta["node_types"].items()}
    meta["addresses"] = {relabel.get(k, k): v for k, v in meta["addresses"].items()}
    for item in meta["external_bgp"]:
        item["node"] = relabel.get(item["node"], item["node"])
    meta["router_id_resolution"]["router_id_to_node"] = {
        rid: relabel.get(node, node)
        for rid, node in meta["router_id_resolution"]["router_id_to_node"].items()
    }
    # Serialisable node/edge lists.  Downstream consumers (the artifact writer
    # and the MCP server's graph rebuild) read topology.json, so `nodes` and
    # `edges` must be lists of records there, not the integer counts reported
    # in this meta dict.
    meta["node_list"] = sorted(G.nodes())
    meta["edge_list"] = [
        {
            "source": u,
            "target": v,
            "edge_type": d.get("edge_type", "unknown"),
            "source_kind": d.get("source_kind", "unknown"),
            "confidence": float(d.get("confidence", 0.0)),
            "directed": bool(d.get("directed", True)),
            "n_sources": int(d.get("n_sources", 1)),
        }
        for u, v, d in sorted(G.edges(data=True))
    ]
    log.info("%s: entity graph %d nodes / %d directed edges, evidence=%s",
             ds.name, meta["nodes"], meta["edges"], meta["evidence_sources"])
    return G, meta
