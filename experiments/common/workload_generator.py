#!/usr/bin/env python3
"""
Config-driven Chakra workload generator for ASTRA-sim.

The generator expands a high-level DAG in workload_spec.json into explicit
Chakra COMM_SEND_NODE / COMM_RECV_NODE / COMP_NODE DAGs, avoiding ASTRA-sim's
native sub-communicator collective execution path.

Currently supported:
  - allreduce + ring
  - alltoall  + direct
  - compute nodes with rank-local dependencies
  - zero-work rank-local join nodes

Preferred workload_spec.json shape:

{
  "num_ranks": 8,
  "nodes": [
    {
      "name": "ar_0",
      "type": "collective",
      "collective": "allreduce",
      "algorithm": "ring",
      "ranks": [0, 1, 4, 5],
      "bytes": 67108864
    },
    {
      "name": "comp_0",
      "type": "compute",
      "ranks": [0, 1, 4, 5],
      "cycles": 1000,
      "depends_on": ["ar_0"]
    }
  ]
}

Only the unified top-level "nodes" DAG is accepted. Fields whose names begin
with "_" are treated as documentation/metadata and ignored.

Optional per-collective fields:
  "routing_label": 0
      Copy label 0 (short) or 1 (long) to every generated SEND/RECV.
      Omit it to retain the network backend's default routing behavior.

  "flow_routing": [
      {"src": 5, "dst": 0, "routing_label": 1}
  ]
      Override the collective label for matching expanded P2P flows. A rule
      may select src, dst, repetition, phase, and/or round. Omitted selector
      fields are wildcards. Exactly one rule may match a flow.

  "repetitions": 1
      Repeat the collective sequentially.

  "depends_on": ["other_collective_name"]
      Add local DAG dependencies on named collectives. This is intended for
      dependencies where the same rank participates in both collectives.

  "bytes_mode": "total_per_rank"
      For alltoall/direct:
        total_per_rank (default): each peer transfer = bytes / group_size
        per_peer:                 each peer transfer = bytes

Notes on semantics:
  * Ring AllReduce uses 2 * (N - 1) rounds:
      N - 1 reduce-scatter rounds
      N - 1 all-gather rounds
    Each network transfer carries bytes / N.
  * Direct All-to-All creates all remote peer exchanges without ring-style
    round dependencies. With bytes_mode=total_per_rank, each peer transfer
    carries bytes / N; the local self-block does not create a network flow.
  * "ranks" order defines Ring order. For [0, 1, 4, 5], the ring is:
      0 -> 1 -> 4 -> 5 -> 0
  * P2P nodes do not need pg_name/comm-group information to execute. A
    comm_group.json is still emitted as experiment metadata and may be passed
    to ASTRA-sim harmlessly.
  * Compute "cycles" is stored in Chakra's duration_micros field, which is the
    runtime field consumed by ASTRA-sim's replay-mode COMP implementation.
  * DAG dependencies are rank-local because Chakra emits one ET per rank. A
    dependency contributes an edge only where parent and child share a rank.

This module owns only DAG parsing and Chakra expansion. Experiment-specific
policy and case selection belong in each experiment's entry point.
"""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


# ---------------------------------------------------------------------------
# Chakra protobuf compatibility
# ---------------------------------------------------------------------------

try:
    from chakra.schema.protobuf.et_def_pb2 import (
        COMM_RECV_NODE,
        COMM_SEND_NODE,
        COMP_NODE,
        INVALID_NODE,
        GlobalMetadata,
        AttributeProto as ChakraAttr,
        Node as ChakraNode,
    )
except ImportError:
    try:
        from chakra.et_def.et_def_pb2 import (
            COMM_RECV_NODE,
            COMM_SEND_NODE,
            COMP_NODE,
            INVALID_NODE,
            GlobalMetadata,
            AttributeProto as ChakraAttr,
            Node as ChakraNode,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Cannot import Chakra protobuf definitions.\n"
            "Install/use the Chakra submodule from the ASTRA-sim checkout, e.g.:\n"
            "  python3 -m pip install -e extern/graph_frontend/chakra\n"
        ) from exc


# ---------------------------------------------------------------------------
# Chakra ET encoding
# ---------------------------------------------------------------------------

def _encode_varint32(out_file, value: int) -> None:
    """Write an unsigned integer using protobuf varint encoding."""
    if value < 0:
        raise ValueError("varint value must be non-negative")

    while value > 0x7F:
        out_file.write(struct.pack("<B", (value & 0x7F) | 0x80))
        value >>= 7
    out_file.write(struct.pack("<B", value))


def encode_message(out_file, message) -> None:
    """
    Chakra ET framing:
      <protobuf-message-length as varint32><protobuf-message-bytes>
    """
    payload = message.SerializeToString()
    _encode_varint32(out_file, len(payload))
    out_file.write(payload)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def normalize_collective_type(value: str) -> str:
    value = value.strip().lower().replace("-", "").replace("_", "")
    aliases = {
        "allreduce": "allreduce",
        "alltoall": "alltoall",
    }
    if value not in aliases:
        raise ValueError(f"Unsupported collective type: {value!r}")
    return aliases[value]


def normalize_algorithm(value: str) -> str:
    return value.strip().lower().replace("-", "").replace("_", "")


def dedup_nodes(nodes: Iterable[ChakraNode]) -> List[ChakraNode]:
    seen = set()
    result: List[ChakraNode] = []
    for node in nodes:
        if node.id not in seen:
            seen.add(node.id)
            result.append(node)
    return result


def require_equal_split(total_bytes: int, parts: int, what: str) -> int:
    if total_bytes <= 0:
        raise ValueError(f"{what}: bytes must be > 0")
    if parts <= 0:
        raise ValueError(f"{what}: split count must be > 0")
    if total_bytes % parts != 0:
        raise ValueError(
            f"{what}: bytes={total_bytes} is not divisible by {parts}. "
            "Use a payload divisible by the communicator size so the explicit "
            "P2P expansion keeps equal-sized ASTRA-style chunks."
        )
    return total_bytes // parts


# ---------------------------------------------------------------------------
# Rank-local ET builder
# ---------------------------------------------------------------------------

class RankBuilder:
    def __init__(self, rank: int):
        self.rank = rank
        self._next_id = 0
        self.nodes: List[ChakraNode] = []

    def _allocate_node(self, name: str, node_type: int) -> ChakraNode:
        node = ChakraNode()
        node.id = self._next_id
        self._next_id += 1
        node.name = name
        node.type = node_type
        return node

    @staticmethod
    def _add_deps(node: ChakraNode, parents: Sequence[ChakraNode]) -> None:
        for parent in dedup_nodes(parents):
            node.data_deps.append(parent.id)

    def send(
        self,
        *,
        dst: int,
        size: int,
        tag: int,
        name: str,
        parents: Sequence[ChakraNode],
        routing_label: Optional[int],
        collective_type: str,
        collective_name: str,
        flow_id: str,
    ) -> ChakraNode:
        node = self._allocate_node(name, COMM_SEND_NODE)
        node.attr.append(ChakraAttr(name="is_cpu_op", bool_val=False))
        node.attr.append(ChakraAttr(name="comm_size", int64_val=size))
        node.attr.append(ChakraAttr(name="comm_src", int32_val=self.rank))
        node.attr.append(ChakraAttr(name="comm_dst", int32_val=dst))
        node.attr.append(ChakraAttr(name="comm_tag", int32_val=tag))
        node.attr.append(
            ChakraAttr(name="collective_type", string_val=collective_type)
        )
        node.attr.append(
            ChakraAttr(name="collective_name", string_val=collective_name)
        )
        node.attr.append(
            ChakraAttr(name="dag_node_name", string_val=collective_name)
        )
        node.attr.append(ChakraAttr(name="flow_id", string_val=flow_id))
        if routing_label is not None:
            node.attr.append(ChakraAttr(name="routing_label", int32_val=routing_label))
        self._add_deps(node, parents)
        self.nodes.append(node)
        return node

    def recv(
        self,
        *,
        src: int,
        size: int,
        tag: int,
        name: str,
        parents: Sequence[ChakraNode],
        routing_label: Optional[int],
        collective_type: str,
        collective_name: str,
        flow_id: str,
    ) -> ChakraNode:
        node = self._allocate_node(name, COMM_RECV_NODE)
        node.attr.append(ChakraAttr(name="is_cpu_op", bool_val=False))
        node.attr.append(ChakraAttr(name="comm_size", int64_val=size))
        node.attr.append(ChakraAttr(name="comm_src", int32_val=src))
        node.attr.append(ChakraAttr(name="comm_dst", int32_val=self.rank))
        node.attr.append(ChakraAttr(name="comm_tag", int32_val=tag))
        node.attr.append(
            ChakraAttr(name="collective_type", string_val=collective_type)
        )
        node.attr.append(
            ChakraAttr(name="collective_name", string_val=collective_name)
        )
        node.attr.append(
            ChakraAttr(name="dag_node_name", string_val=collective_name)
        )
        node.attr.append(ChakraAttr(name="flow_id", string_val=flow_id))
        if routing_label is not None:
            node.attr.append(ChakraAttr(name="routing_label", int32_val=routing_label))
        self._add_deps(node, parents)
        self.nodes.append(node)
        return node

    def compute(
        self,
        *,
        name: str,
        dag_node_name: str,
        cycles: int,
        parents: Sequence[ChakraNode],
    ) -> ChakraNode:
        """Add a rank-local Chakra compute node."""
        node = self._allocate_node(name, COMP_NODE)
        node.attr.append(ChakraAttr(name="is_cpu_op", bool_val=False))
        node.attr.append(
            ChakraAttr(name="dag_node_name", string_val=dag_node_name)
        )
        node.duration_micros = cycles
        self._add_deps(node, parents)
        self.nodes.append(node)
        return node

    def join(
        self,
        *,
        name: str,
        dag_node_name: str,
        parents: Sequence[ChakraNode],
    ) -> ChakraNode:
        """Add an instantaneous rank-local DAG join."""
        node = self._allocate_node(name, INVALID_NODE)
        node.attr.append(ChakraAttr(name="is_cpu_op", bool_val=False))
        node.attr.append(
            ChakraAttr(name="dag_node_name", string_val=dag_node_name)
        )
        self._add_deps(node, parents)
        self.nodes.append(node)
        return node

    def idle_node(self) -> ChakraNode:
        """
        Add a zero-work COMP node if a rank has no communication nodes.
        This keeps every rank ET non-empty for generic experiments.
        """
        node = self._allocate_node("IDLE_RANK", COMP_NODE)
        node.attr.append(ChakraAttr(name="is_cpu_op", bool_val=False))
        node.attr.append(ChakraAttr(name="num_ops", int64_val=0))
        node.attr.append(ChakraAttr(name="tensor_size", uint64_val=0))
        self.nodes.append(node)
        return node

    def write(self, output_file: Path) -> None:
        with output_file.open("wb") as f:
            encode_message(f, GlobalMetadata(version="1.0.0"))
            for node in self.nodes:
                encode_message(f, node)


class TagAllocator:
    """
    Allocate deterministic, globally unique positive int32 tags.

    Matching SEND/RECV endpoints receive the same tag.
    """
    def __init__(self, start: int = 1):
        self._next = start

    def next(self) -> int:
        if self._next >= 2**31:
            raise OverflowError("comm_tag exceeded signed int32 range")
        tag = self._next
        self._next += 1
        return tag


# ---------------------------------------------------------------------------
# DAG node specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlowRoutingRule:
    routing_label: int
    src: Optional[int] = None
    dst: Optional[int] = None
    repetition: Optional[int] = None
    phase: Optional[str] = None
    round_index: Optional[int] = None

    def matches(
        self,
        *,
        src: int,
        dst: int,
        repetition: int,
        phase: str,
        round_index: Optional[int],
    ) -> bool:
        return (
            (self.src is None or self.src == src)
            and (self.dst is None or self.dst == dst)
            and (self.repetition is None or self.repetition == repetition)
            and (self.phase is None or self.phase == phase)
            and (self.round_index is None or self.round_index == round_index)
        )

    def to_json(self) -> dict:
        result = {"routing_label": self.routing_label}
        for key, value in (
            ("src", self.src),
            ("dst", self.dst),
            ("repetition", self.repetition),
            ("phase", self.phase),
            ("round", self.round_index),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True)
class CollectiveSpec:
    name: str
    type: str
    algorithm: str
    ranks: Tuple[int, ...]
    bytes: int
    repetitions: int = 1
    depends_on: Tuple[str, ...] = ()
    bytes_mode: str = "total_per_rank"
    routing_label: Optional[int] = None
    flow_routing: Tuple[FlowRoutingRule, ...] = ()


@dataclass(frozen=True)
class ComputeSpec:
    name: str
    ranks: Tuple[int, ...]
    cycles: int
    depends_on: Tuple[str, ...] = ()


@dataclass(frozen=True)
class JoinSpec:
    name: str
    depends_on: Tuple[str, ...]


NodeSpec = Union[CollectiveSpec, ComputeSpec, JoinSpec]


def parse_dependencies(raw: Mapping) -> Tuple[str, ...]:
    depends_on_raw = raw.get("depends_on", [])
    if isinstance(depends_on_raw, str):
        return (depends_on_raw,)
    return tuple(str(value) for value in depends_on_raw)


def parse_ranks(raw: Mapping, num_ranks: int, name: str) -> Tuple[int, ...]:
    if "ranks" not in raw:
        raise ValueError(f"{name}: missing required field 'ranks'")
    ranks = tuple(int(value) for value in raw["ranks"])
    if not ranks:
        raise ValueError(f"{name}: ranks must not be empty")
    if len(set(ranks)) != len(ranks):
        raise ValueError(f"{name}: ranks must be unique; got {ranks}")
    for rank in ranks:
        if rank < 0 or rank >= num_ranks:
            raise ValueError(
                f"{name}: rank {rank} is outside valid range [0, {num_ranks - 1}]"
            )
    return ranks


def parse_flow_routing(
    raw: Mapping,
    *,
    name: str,
    ranks: Tuple[int, ...],
    repetitions: int,
) -> Tuple[FlowRoutingRule, ...]:
    raw_rules = raw.get("flow_routing", [])
    if not isinstance(raw_rules, list):
        raise ValueError(f"{name}: flow_routing must be a list")

    allowed = {
        "routing_label", "src", "dst", "repetition", "phase", "round"
    }
    rules = []
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, Mapping):
            raise ValueError(f"{name}: flow_routing[{index}] must be an object")
        unknown = set(raw_rule) - allowed
        if unknown:
            raise ValueError(
                f"{name}: flow_routing[{index}] has unknown fields {sorted(unknown)}"
            )
        selectors = set(raw_rule) - {"routing_label"}
        if not selectors:
            raise ValueError(
                f"{name}: flow_routing[{index}] must select at least one flow field"
            )
        label = int(raw_rule.get("routing_label", -1))
        if label not in (0, 1):
            raise ValueError(
                f"{name}: flow_routing[{index}].routing_label must be 0 or 1"
            )
        src = int(raw_rule["src"]) if "src" in raw_rule else None
        dst = int(raw_rule["dst"]) if "dst" in raw_rule else None
        repetition = (
            int(raw_rule["repetition"]) if "repetition" in raw_rule else None
        )
        round_index = int(raw_rule["round"]) if "round" in raw_rule else None
        if src is not None and src not in ranks:
            raise ValueError(f"{name}: flow_routing[{index}] src is outside ranks")
        if dst is not None and dst not in ranks:
            raise ValueError(f"{name}: flow_routing[{index}] dst is outside ranks")
        if repetition is not None and not 0 <= repetition < repetitions:
            raise ValueError(f"{name}: flow_routing[{index}] repetition is invalid")
        if round_index is not None and round_index < 0:
            raise ValueError(f"{name}: flow_routing[{index}] round must be >= 0")
        phase = raw_rule.get("phase")
        if phase is not None:
            phase = str(phase).strip().lower().replace("-", "_")
        rules.append(FlowRoutingRule(label, src, dst, repetition, phase, round_index))
    return tuple(rules)


def parse_collective(raw: Mapping, num_ranks: int) -> CollectiveSpec:
    required = ("name", "type", "collective", "algorithm", "ranks", "bytes")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"Collective is missing required fields: {missing}")

    name = str(raw["name"]).strip()
    if not name:
        raise ValueError("Collective name cannot be empty")

    coll_type = normalize_collective_type(str(raw["collective"]))
    algorithm = normalize_algorithm(str(raw["algorithm"]))

    ranks = parse_ranks(raw, num_ranks, name)
    if len(ranks) < 2:
        raise ValueError(f"{name}: collective must contain at least 2 ranks")

    total_bytes = int(raw["bytes"])
    if total_bytes <= 0:
        raise ValueError(f"{name}: bytes must be > 0")

    repetitions = int(raw.get("repetitions", 1))
    if repetitions <= 0:
        raise ValueError(f"{name}: repetitions must be > 0")

    depends_on = parse_dependencies(raw)

    bytes_mode = str(raw.get("bytes_mode", "total_per_rank")).strip().lower()
    if bytes_mode not in ("total_per_rank", "per_peer"):
        raise ValueError(
            f"{name}: bytes_mode must be 'total_per_rank' or 'per_peer'"
        )

    routing_label_raw = raw.get("routing_label")
    routing_label = None if routing_label_raw is None else int(routing_label_raw)
    if routing_label not in (None, 0, 1):
        raise ValueError(f"{name}: routing_label must be 0 or 1 when specified")

    return CollectiveSpec(
        name=name,
        type=coll_type,
        algorithm=algorithm,
        ranks=ranks,
        bytes=total_bytes,
        repetitions=repetitions,
        depends_on=depends_on,
        bytes_mode=bytes_mode,
        routing_label=routing_label,
        flow_routing=parse_flow_routing(
            raw, name=name, ranks=ranks, repetitions=repetitions
        ),
    )


def parse_compute(raw: Mapping, num_ranks: int) -> ComputeSpec:
    required = ("name", "type", "ranks", "cycles")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"Compute node is missing required fields: {missing}")

    name = str(raw["name"]).strip()
    if not name:
        raise ValueError("Compute node name cannot be empty")

    cycles = int(raw["cycles"])
    if cycles < 0:
        raise ValueError(f"{name}: cycles must be >= 0")

    return ComputeSpec(
        name=name,
        ranks=parse_ranks(raw, num_ranks, name),
        cycles=cycles,
        depends_on=parse_dependencies(raw),
    )


def parse_join(raw: Mapping) -> JoinSpec:
    required = ("name", "type", "depends_on")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"Join node is missing required fields: {missing}")

    name = str(raw["name"]).strip()
    if not name:
        raise ValueError("Join node name cannot be empty")
    depends_on = parse_dependencies(raw)
    if not depends_on:
        raise ValueError(f"{name}: join depends_on must not be empty")
    return JoinSpec(name=name, depends_on=depends_on)


def parse_node(raw: Mapping, num_ranks: int) -> NodeSpec:
    if "type" not in raw:
        raise ValueError("DAG node is missing required field 'type'")
    node_type = str(raw["type"]).strip().lower()
    if node_type == "collective":
        return parse_collective(raw, num_ranks)
    if node_type == "compute":
        return parse_compute(raw, num_ranks)
    if node_type == "join":
        return parse_join(raw)
    raise ValueError(f"Unsupported DAG node type: {raw['type']!r}")


def topological_node_order(specs: Sequence[NodeSpec]) -> List[NodeSpec]:
    """
    Topologically order high-level DAG nodes using depends_on.
    Independent nodes remain free to execute concurrently.
    """
    by_name = {spec.name: spec for spec in specs}
    if len(by_name) != len(specs):
        raise ValueError("DAG node names must be unique")

    for spec in specs:
        for parent in spec.depends_on:
            if parent not in by_name:
                raise ValueError(
                    f"{spec.name}: depends_on references unknown node {parent!r}"
                )

    state: Dict[str, int] = {}  # 0 absent, 1 visiting, 2 done
    ordered: List[NodeSpec] = []

    def visit(name: str) -> None:
        current = state.get(name, 0)
        if current == 1:
            raise ValueError(f"Cycle detected in DAG dependencies at {name!r}")
        if current == 2:
            return

        state[name] = 1
        spec = by_name[name]
        for parent in spec.depends_on:
            visit(parent)
        state[name] = 2
        ordered.append(spec)

    for spec in specs:
        visit(spec.name)

    return ordered


# ---------------------------------------------------------------------------
# Expansion context
# ---------------------------------------------------------------------------

class ExpansionContext:
    def __init__(self, num_ranks: int):
        self.builders: Dict[int, RankBuilder] = {
            rank: RankBuilder(rank) for rank in range(num_ranks)
        }
        self.tags = TagAllocator()

        # collective_name -> rank -> terminal nodes
        self.terminals: Dict[str, Dict[int, List[ChakraNode]]] = {}

        # Summary counters
        self.flow_count: Dict[str, int] = {}
        self.network_bytes: Dict[str, int] = {}
        self.flow_plan: List[dict] = []
        self.rule_matches: Dict[Tuple[str, int], int] = {}

    def dependency_frontier(
        self,
        ranks: Sequence[int],
        depends_on: Sequence[str],
    ) -> Dict[int, List[ChakraNode]]:
        """
        Build a rank-local starting frontier from explicit depends_on edges.

        If a dependency node does not contain a particular child rank, no
        local edge can be emitted for that rank. This helper never creates
        cross-rank synchronization because Chakra ET dependencies are local.
        """
        result: Dict[int, List[ChakraNode]] = {rank: [] for rank in ranks}

        for parent_name in depends_on:
            parent_terminals = self.terminals[parent_name]
            for rank in ranks:
                result[rank].extend(parent_terminals.get(rank, []))

        return {rank: dedup_nodes(nodes) for rank, nodes in result.items()}

    def record_flow(self, collective_name: str, size: int) -> None:
        self.flow_count[collective_name] = (
            self.flow_count.get(collective_name, 0) + 1
        )
        self.network_bytes[collective_name] = (
            self.network_bytes.get(collective_name, 0) + size
        )

    def p2p(
        self,
        *,
        spec: CollectiveSpec,
        src: int,
        dst: int,
        size: int,
        send_parents: Sequence[ChakraNode],
        recv_parents: Sequence[ChakraNode],
        op_name: str,
        repetition: int,
        phase: str,
        round_index: Optional[int] = None,
    ) -> Tuple[ChakraNode, ChakraNode]:
        matching_rules = [
            (index, rule)
            for index, rule in enumerate(spec.flow_routing)
            if rule.matches(
                src=src,
                dst=dst,
                repetition=repetition,
                phase=phase,
                round_index=round_index,
            )
        ]
        if len(matching_rules) > 1:
            indices = [index for index, unused in matching_rules]
            raise ValueError(
                f"{spec.name}: flow {src}->{dst} matches multiple "
                f"flow_routing rules {indices}"
            )
        routing_label = spec.routing_label
        if matching_rules:
            rule_index, rule = matching_rules[0]
            routing_label = rule.routing_label
            key = (spec.name, rule_index)
            self.rule_matches[key] = self.rule_matches.get(key, 0) + 1

        flow_id = f"{spec.name}/rep{repetition}/{phase}"
        if round_index is not None:
            flow_id += f"/round{round_index}"
        flow_id += f"/{src}-{dst}"
        tag = self.tags.next()

        send_node = self.builders[src].send(
            dst=dst,
            size=size,
            tag=tag,
            name=f"{op_name}_SEND_{src}_TO_{dst}",
            parents=send_parents,
            routing_label=routing_label,
            collective_type=spec.type,
            collective_name=spec.name,
            flow_id=flow_id,
        )

        recv_node = self.builders[dst].recv(
            src=src,
            size=size,
            tag=tag,
            name=f"{op_name}_RECV_{src}_TO_{dst}",
            parents=recv_parents,
            routing_label=routing_label,
            collective_type=spec.type,
            collective_name=spec.name,
            flow_id=flow_id,
        )

        self.record_flow(spec.name, size)
        self.flow_plan.append({
            "flow_id": flow_id,
            "collective": spec.name,
            "src": src,
            "dst": dst,
            "bytes": size,
            "tag": tag,
            "routing_label": routing_label,
        })
        return send_node, recv_node


# ---------------------------------------------------------------------------
# Collective expanders
# ---------------------------------------------------------------------------

def expand_ring_allreduce(
    ctx: ExpansionContext,
    spec: CollectiveSpec,
    initial_frontier: Dict[int, List[ChakraNode]],
    repetition: int,
) -> Dict[int, List[ChakraNode]]:
    """
    Explicit ring AllReduce.

    For N ranks:
      reduce-scatter: N - 1 rounds
      all-gather:     N - 1 rounds

    Every rank sends one chunk to its next ring neighbor and receives one
    chunk from its previous neighbor per round.

    The rank ordering in spec.ranks is the ring ordering.
    """
    ranks = list(spec.ranks)
    n = len(ranks)
    chunk_bytes = require_equal_split(
        spec.bytes,
        n,
        f"{spec.name} ring AllReduce",
    )

    frontier: Dict[int, List[ChakraNode]] = {
        rank: list(initial_frontier.get(rank, [])) for rank in ranks
    }

    phases = (
        ("RS", "reduce_scatter", n - 1),
        ("AG", "all_gather", n - 1),
    )

    for phase_short, phase_name, num_rounds in phases:
        for round_idx in range(num_rounds):
            previous = {
                rank: list(frontier[rank])
                for rank in ranks
            }
            current: Dict[int, List[ChakraNode]] = {
                rank: [] for rank in ranks
            }

            # One directed ring flow from every rank to its next neighbor.
            for index, src in enumerate(ranks):
                dst = ranks[(index + 1) % n]

                send_node, recv_node = ctx.p2p(
                    spec=spec,
                    src=src,
                    dst=dst,
                    size=chunk_bytes,
                    send_parents=previous[src],
                    recv_parents=previous[dst],
                    op_name=(
                        f"{spec.name}_REP{repetition}_"
                        f"{phase_short}_ROUND{round_idx}"
                    ),
                    repetition=repetition,
                    phase=phase_name,
                    round_index=round_idx,
                )

                current[src].append(send_node)
                current[dst].append(recv_node)

            # A rank enters the next Ring round only after both its current
            # SEND and RECV have completed.
            frontier = {
                rank: dedup_nodes(current[rank])
                for rank in ranks
            }

    return frontier


def expand_direct_alltoall(
    ctx: ExpansionContext,
    spec: CollectiveSpec,
    initial_frontier: Dict[int, List[ChakraNode]],
    repetition: int,
) -> Dict[int, List[ChakraNode]]:
    """
    Explicit direct All-to-All.

    All remote peer exchanges are independent at the collective DAG level:
    there is no Ring-style inter-peer round dependency.

    bytes_mode:
      total_per_rank:
          bytes is the full per-rank All-to-All input payload.
          It is split into N equal destination blocks, including the local
          self-block; therefore each remote P2P flow carries bytes / N.

      per_peer:
          bytes is already the number of bytes sent to each remote peer.
    """
    ranks = list(spec.ranks)
    n = len(ranks)

    if spec.bytes_mode == "total_per_rank":
        peer_bytes = require_equal_split(
            spec.bytes,
            n,
            f"{spec.name} direct AllToAll",
        )
    else:
        peer_bytes = spec.bytes

    current: Dict[int, List[ChakraNode]] = {
        rank: [] for rank in ranks
    }

    # Every directed remote src->dst pair becomes one matched SEND/RECV pair.
    # All of them share only the collective's initial dependencies.
    for src in ranks:
        for dst in ranks:
            if src == dst:
                continue

            send_node, recv_node = ctx.p2p(
                spec=spec,
                src=src,
                dst=dst,
                size=peer_bytes,
                send_parents=initial_frontier.get(src, []),
                recv_parents=initial_frontier.get(dst, []),
                op_name=f"{spec.name}_REP{repetition}_DIRECT",
                repetition=repetition,
                phase="direct",
            )

            current[src].append(send_node)
            current[dst].append(recv_node)

    return {
        rank: dedup_nodes(current[rank])
        for rank in ranks
    }


EXPANDERS = {
    ("allreduce", "ring"): expand_ring_allreduce,
    ("alltoall", "direct"): expand_direct_alltoall,
}


# ---------------------------------------------------------------------------
# Workload generation
# ---------------------------------------------------------------------------

def expand_collective(
    ctx: ExpansionContext,
    spec: CollectiveSpec,
) -> None:
    key = (spec.type, spec.algorithm)
    if key not in EXPANDERS:
        supported = ", ".join(
            f"{coll}/{algo}" for coll, algo in sorted(EXPANDERS)
        )
        raise ValueError(
            f"{spec.name}: unsupported collective/algorithm "
            f"{spec.type}/{spec.algorithm}. Supported: {supported}"
        )

    expander = EXPANDERS[key]
    frontier = ctx.dependency_frontier(spec.ranks, spec.depends_on)

    # Repetitions of the same collective are sequential. Independent
    # collectives remain concurrent unless depends_on says otherwise.
    for repetition in range(spec.repetitions):
        frontier = expander(
            ctx,
            spec,
            frontier,
            repetition,
        )

    unmatched = [
        index
        for index in range(len(spec.flow_routing))
        if ctx.rule_matches.get((spec.name, index), 0) == 0
    ]
    if unmatched:
        raise ValueError(
            f"{spec.name}: flow_routing rules {unmatched} matched no expanded flow"
        )

    ctx.terminals[spec.name] = {
        rank: list(frontier.get(rank, []))
        for rank in spec.ranks
    }


def expand_compute(ctx: ExpansionContext, spec: ComputeSpec) -> None:
    frontier = ctx.dependency_frontier(spec.ranks, spec.depends_on)
    ctx.terminals[spec.name] = {
        rank: [ctx.builders[rank].compute(
            name=f"{spec.name}_COMP_RANK{rank}",
            dag_node_name=spec.name,
            cycles=spec.cycles,
            parents=frontier[rank],
        )]
        for rank in spec.ranks
    }


def expand_join(ctx: ExpansionContext, spec: JoinSpec) -> None:
    # A Chakra ET is rank-local. Materialize the join on the union of its
    # parents' ranks, with edges from every parent present on each rank.
    ranks = sorted({
        rank
        for parent_name in spec.depends_on
        for rank in ctx.terminals[parent_name]
    })
    frontier = ctx.dependency_frontier(ranks, spec.depends_on)
    ctx.terminals[spec.name] = {
        rank: [ctx.builders[rank].join(
            name=f"{spec.name}_JOIN_RANK{rank}",
            dag_node_name=spec.name,
            parents=frontier[rank],
        )]
        for rank in ranks
    }


def expand_node(ctx: ExpansionContext, spec: NodeSpec) -> None:
    if isinstance(spec, CollectiveSpec):
        expand_collective(ctx, spec)
    elif isinstance(spec, ComputeSpec):
        expand_compute(ctx, spec)
    else:
        expand_join(ctx, spec)


def remove_private_fields(value):
    """Recursively discard schema/comments whose object key starts with '_'."""
    if isinstance(value, dict):
        return {
            key: remove_private_fields(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [remove_private_fields(item) for item in value]
    return value


def load_spec(path: Path) -> Tuple[int, List[NodeSpec]]:
    with path.open("r", encoding="utf-8") as f:
        raw = remove_private_fields(json.load(f))

    if "num_ranks" not in raw:
        raise ValueError("workload_spec.json must contain 'num_ranks'")
    if "nodes" not in raw:
        raise ValueError("workload_spec.json must contain 'nodes'")

    num_ranks = int(raw["num_ranks"])
    if num_ranks <= 0:
        raise ValueError("num_ranks must be > 0")

    specs = [
        parse_node(item, num_ranks)
        for item in raw["nodes"]
    ]
    if not specs:
        raise ValueError("nodes must not be empty")

    return num_ranks, topological_node_order(specs)


def write_comm_group_metadata(
    output_dir: Path,
    specs: Sequence[NodeSpec],
) -> None:
    """
    Emit communicator metadata.

    Explicit P2P ET nodes do not consume pg_name, so this file is not required
    for correctness. It is emitted to preserve the experiment's communicator
    description and to keep existing ASTRA-sim command lines usable.
    """
    collectives = [spec for spec in specs if isinstance(spec, CollectiveSpec)]
    groups = {
        str(index): list(spec.ranks)
        for index, spec in enumerate(collectives)
    }

    destination = output_dir / "comm_group.json"
    temporary = output_dir / ".comm_group.json.tmp"
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(groups, f, indent=2)
        f.write("\n")
    temporary.replace(destination)


def write_resolved_spec(
    output_path: Path,
    num_ranks: int,
    specs: Sequence[NodeSpec],
) -> None:
    """Write the normalized, post-override experiment specification."""
    nodes = []
    for spec in specs:
        if isinstance(spec, CollectiveSpec):
            item = {
                "name": spec.name,
                "type": "collective",
                "collective": spec.type,
                "algorithm": spec.algorithm,
                "ranks": list(spec.ranks),
                "bytes": spec.bytes,
                "repetitions": spec.repetitions,
                "depends_on": list(spec.depends_on),
                "bytes_mode": spec.bytes_mode,
            }
            if spec.routing_label is not None:
                item["routing_label"] = spec.routing_label
            if spec.flow_routing:
                item["flow_routing"] = [
                    rule.to_json() for rule in spec.flow_routing
                ]
        elif isinstance(spec, ComputeSpec):
            item = {
                "name": spec.name,
                "type": "compute",
                "ranks": list(spec.ranks),
                "cycles": spec.cycles,
                "depends_on": list(spec.depends_on),
            }
        else:
            item = {
                "name": spec.name,
                "type": "join",
                "depends_on": list(spec.depends_on),
            }
        nodes.append(item)

    resolved = {
        "num_ranks": num_ranks,
        "nodes": nodes,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(resolved, f, indent=2)
        f.write("\n")
    temporary.replace(output_path)


def write_flow_plan(output_dir: Path, ctx: ExpansionContext) -> None:
    destination = output_dir / "flow_plan.json"
    temporary = output_dir / ".flow_plan.json.tmp"
    with temporary.open("w", encoding="utf-8") as f:
        json.dump({"flows": ctx.flow_plan}, f, indent=2)
        f.write("\n")
    temporary.replace(destination)


def remove_old_et_files(output_dir: Path) -> None:
    for path in output_dir.glob("workload.*.et"):
        path.unlink()


def main(default_exp_dir: Optional[Path] = None) -> None:
    exp_dir = (
        default_exp_dir.resolve()
        if default_exp_dir is not None
        else Path.cwd()
    )

    parser = argparse.ArgumentParser(
        description="Expand a high-level DAG into explicit Chakra ET files."
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=exp_dir / "workload_spec.json",
        help="Workload specification JSON (default: workload_spec.json)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=exp_dir / "log" / "manual_generate" / "workload",
        help="Output directory (default: log/manual_generate/workload)",
    )
    routing_mode = parser.add_mutually_exclusive_group()
    routing_mode.add_argument(
        "--routing-label-override",
        type=int,
        choices=(0, 1),
        help="Override every collective routing_label.",
    )
    routing_mode.add_argument(
        "--no-routing-label",
        action="store_true",
        help="Remove routing_label from every collective to use backend routing.",
    )
    routing_mode.add_argument(
        "--no-flow-routing",
        action="store_true",
        help="Ignore flow_routing overrides and keep collective labels.",
    )
    routing_mode.add_argument(
        "--invert-routing-labels",
        action="store_true",
        help="Invert every configured routing_label (0 becomes 1 and 1 becomes 0).",
    )
    parser.add_argument(
        "--resolved-spec-output",
        type=Path,
        help="Write the normalized, post-override specification to this path.",
    )
    parser.add_argument(
        "--compute-cycles-override",
        type=int,
        help="Override cycles for every compute node (used by run_sweep.sh).",
    )
    args = parser.parse_args()

    spec_path = args.spec.resolve()
    output_dir = args.output_dir.resolve()

    num_ranks, specs = load_spec(spec_path)
    if args.compute_cycles_override is not None:
        if args.compute_cycles_override < 0:
            raise ValueError("--compute-cycles-override must be >= 0")
        specs = [
            replace(spec, cycles=args.compute_cycles_override)
            if isinstance(spec, ComputeSpec) else spec
            for spec in specs
        ]

    if args.routing_label_override is not None:
        specs = [
            replace(spec, routing_label=args.routing_label_override, flow_routing=())
            if isinstance(spec, CollectiveSpec) else spec
            for spec in specs
        ]
    elif args.no_routing_label:
        specs = [
            replace(spec, routing_label=None, flow_routing=())
            if isinstance(spec, CollectiveSpec) else spec
            for spec in specs
        ]
    elif args.no_flow_routing:
        specs = [
            replace(spec, flow_routing=())
            if isinstance(spec, CollectiveSpec) else spec
            for spec in specs
        ]
    elif args.invert_routing_labels:
        collectives = [
            spec for spec in specs if isinstance(spec, CollectiveSpec)
        ]
        if any(spec.routing_label not in (0, 1) for spec in collectives):
            raise ValueError(
                "--invert-routing-labels requires every collective to define "
                "routing_label 0 or 1"
            )
        specs = [
            replace(
                spec,
                routing_label=1 - spec.routing_label,
                flow_routing=tuple(
                    replace(rule, routing_label=1 - rule.routing_label)
                    for rule in spec.flow_routing
                ),
            )
            if isinstance(spec, CollectiveSpec) else spec
            for spec in specs
        ]

    if args.resolved_spec_output is not None:
        write_resolved_spec(
            args.resolved_spec_output.resolve(),
            num_ranks,
            specs,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    remove_old_et_files(output_dir)

    ctx = ExpansionContext(num_ranks)

    for spec in specs:
        expand_node(ctx, spec)

    write_flow_plan(output_dir, ctx)

    # Keep ET files non-empty even when a generic future experiment has ranks
    # outside all collectives.
    for rank, builder in ctx.builders.items():
        if not builder.nodes:
            builder.idle_node()

    for rank in range(num_ranks):
        ctx.builders[rank].write(output_dir / f"workload.{rank}.et")

    write_comm_group_metadata(output_dir, specs)

    print(f"Generated Chakra workload from: {spec_path}")
    print(f"Output directory:               {output_dir}")
    print(f"Number of ranks:                {num_ranks}")
    print()

    for spec in specs:
        if isinstance(spec, CollectiveSpec):
            flows = ctx.flow_count.get(spec.name, 0)
            net_bytes = ctx.network_bytes.get(spec.name, 0)
            print(
                f"[{spec.name}] collective {spec.type}/{spec.algorithm} "
                f"ranks={list(spec.ranks)} bytes={spec.bytes} "
                f"repetitions={spec.repetitions}"
            )

            if spec.type == "allreduce" and spec.algorithm == "ring":
                n = len(spec.ranks)
                print(
                    f"  ring order:       {' -> '.join(map(str, spec.ranks))} "
                    f"-> {spec.ranks[0]}"
                )
                print(f"  rounds/repetition:{2 * (n - 1)}")
                print(f"  chunk bytes:      {spec.bytes // n}")

            elif spec.type == "alltoall" and spec.algorithm == "direct":
                if spec.bytes_mode == "total_per_rank":
                    print(f"  peer bytes:       {spec.bytes // len(spec.ranks)}")
                else:
                    print(f"  peer bytes:       {spec.bytes}")
                print("  peer scheduling:  concurrent/no ring dependency")

            print(f"  network flows:    {flows}")
            print(f"  network bytes:    {net_bytes}")
        elif isinstance(spec, ComputeSpec):
            print(
                f"[{spec.name}] compute ranks={list(spec.ranks)} "
                f"cycles={spec.cycles} depends_on={list(spec.depends_on)}"
            )
        else:
            ranks = sorted(ctx.terminals[spec.name])
            print(
                f"[{spec.name}] join ranks={ranks} "
                f"depends_on={list(spec.depends_on)}"
            )
        print()

    print("ASTRA workload prefix:")
    print(f"  {output_dir / 'workload'}")
    print()
    print("Communicator metadata:")
    print(f"  {output_dir / 'comm_group.json'}")


if __name__ == "__main__":
    main()
