#!/usr/bin/env python3
"""
Config-driven Chakra P2P workload generator for ASTRA-sim.

The generator expands collective descriptions in workload_spec.json into
explicit COMM_SEND_NODE / COMM_RECV_NODE DAGs, avoiding ASTRA-sim's native
sub-communicator collective execution path.

Currently supported:
  - allreduce + ring
  - alltoall  + direct

Default directory layout:

  exp1/
  ├── generate_workload.py
  ├── workload_spec.json
  └── workload/
      ├── workload.0.et
      ├── ...
      ├── workload.<N-1>.et
      └── comm_group.json

Expected workload_spec.json:

{
  "num_ranks": 8,
  "collectives": [
    {
      "name": "ar_0",
      "type": "allreduce",
      "algorithm": "ring",
      "ranks": [0, 1, 4, 5],
      "bytes": 67108864
    },
    {
      "name": "a2a_0",
      "type": "alltoall",
      "algorithm": "direct",
      "ranks": [2, 3, 6, 7],
      "bytes": 67108864
    }
  ]
}

Optional per-collective fields:
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

Usage:
  python3 generate_workload.py

Optional:
  python3 generate_workload.py \
      --spec workload_spec.json \
      --output-dir workload
"""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Chakra protobuf compatibility
# ---------------------------------------------------------------------------

try:
    from chakra.schema.protobuf.et_def_pb2 import (
        COMM_RECV_NODE,
        COMM_SEND_NODE,
        COMP_NODE,
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
    ) -> ChakraNode:
        node = self._allocate_node(name, COMM_SEND_NODE)
        node.attr.append(ChakraAttr(name="is_cpu_op", bool_val=False))
        node.attr.append(ChakraAttr(name="comm_size", int64_val=size))
        node.attr.append(ChakraAttr(name="comm_src", int32_val=self.rank))
        node.attr.append(ChakraAttr(name="comm_dst", int32_val=dst))
        node.attr.append(ChakraAttr(name="comm_tag", int32_val=tag))
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
    ) -> ChakraNode:
        node = self._allocate_node(name, COMM_RECV_NODE)
        node.attr.append(ChakraAttr(name="is_cpu_op", bool_val=False))
        node.attr.append(ChakraAttr(name="comm_size", int64_val=size))
        node.attr.append(ChakraAttr(name="comm_src", int32_val=src))
        node.attr.append(ChakraAttr(name="comm_dst", int32_val=self.rank))
        node.attr.append(ChakraAttr(name="comm_tag", int32_val=tag))
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
# Collective specification
# ---------------------------------------------------------------------------

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


def parse_collective(raw: Mapping, num_ranks: int) -> CollectiveSpec:
    required = ("name", "type", "algorithm", "ranks", "bytes")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"Collective is missing required fields: {missing}")

    name = str(raw["name"]).strip()
    if not name:
        raise ValueError("Collective name cannot be empty")

    coll_type = normalize_collective_type(str(raw["type"]))
    algorithm = normalize_algorithm(str(raw["algorithm"]))

    ranks = tuple(int(x) for x in raw["ranks"])
    if len(ranks) < 2:
        raise ValueError(f"{name}: collective must contain at least 2 ranks")
    if len(set(ranks)) != len(ranks):
        raise ValueError(f"{name}: ranks must be unique; got {ranks}")
    for rank in ranks:
        if rank < 0 or rank >= num_ranks:
            raise ValueError(
                f"{name}: rank {rank} is outside valid range [0, {num_ranks - 1}]"
            )

    total_bytes = int(raw["bytes"])
    if total_bytes <= 0:
        raise ValueError(f"{name}: bytes must be > 0")

    repetitions = int(raw.get("repetitions", 1))
    if repetitions <= 0:
        raise ValueError(f"{name}: repetitions must be > 0")

    depends_on_raw = raw.get("depends_on", [])
    if isinstance(depends_on_raw, str):
        depends_on = (depends_on_raw,)
    else:
        depends_on = tuple(str(x) for x in depends_on_raw)

    bytes_mode = str(raw.get("bytes_mode", "total_per_rank")).strip().lower()
    if bytes_mode not in ("total_per_rank", "per_peer"):
        raise ValueError(
            f"{name}: bytes_mode must be 'total_per_rank' or 'per_peer'"
        )

    return CollectiveSpec(
        name=name,
        type=coll_type,
        algorithm=algorithm,
        ranks=ranks,
        bytes=total_bytes,
        repetitions=repetitions,
        depends_on=depends_on,
        bytes_mode=bytes_mode,
    )


def topological_collective_order(
    specs: Sequence[CollectiveSpec],
) -> List[CollectiveSpec]:
    """
    Topologically order collectives using depends_on.
    Independent collectives remain free to execute concurrently because no
    DAG edges are added between them.
    """
    by_name = {spec.name: spec for spec in specs}
    if len(by_name) != len(specs):
        raise ValueError("Collective names must be unique")

    for spec in specs:
        for parent in spec.depends_on:
            if parent not in by_name:
                raise ValueError(
                    f"{spec.name}: depends_on references unknown collective {parent!r}"
                )

    state: Dict[str, int] = {}  # 0 absent, 1 visiting, 2 done
    ordered: List[CollectiveSpec] = []

    def visit(name: str) -> None:
        current = state.get(name, 0)
        if current == 1:
            raise ValueError(f"Cycle detected in collective dependencies at {name!r}")
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

    def dependency_frontier(
        self,
        spec: CollectiveSpec,
    ) -> Dict[int, List[ChakraNode]]:
        """
        Build a rank-local starting frontier from explicit depends_on edges.

        If a dependency collective does not contain a particular child rank,
        no local edge can be emitted for that rank. For cross-rank joins/barriers,
        add an explicit synchronization representation in a future expander
        rather than relying on this local dependency helper.
        """
        result: Dict[int, List[ChakraNode]] = {rank: [] for rank in spec.ranks}

        for parent_name in spec.depends_on:
            parent_terminals = self.terminals[parent_name]
            for rank in spec.ranks:
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
        collective_name: str,
        src: int,
        dst: int,
        size: int,
        send_parents: Sequence[ChakraNode],
        recv_parents: Sequence[ChakraNode],
        op_name: str,
    ) -> Tuple[ChakraNode, ChakraNode]:
        tag = self.tags.next()

        send_node = self.builders[src].send(
            dst=dst,
            size=size,
            tag=tag,
            name=f"{op_name}_SEND_{src}_TO_{dst}",
            parents=send_parents,
        )

        recv_node = self.builders[dst].recv(
            src=src,
            size=size,
            tag=tag,
            name=f"{op_name}_RECV_{src}_TO_{dst}",
            parents=recv_parents,
        )

        self.record_flow(collective_name, size)
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
                    collective_name=spec.name,
                    src=src,
                    dst=dst,
                    size=chunk_bytes,
                    send_parents=previous[src],
                    recv_parents=previous[dst],
                    op_name=(
                        f"{spec.name}_REP{repetition}_"
                        f"{phase_short}_ROUND{round_idx}"
                    ),
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
                collective_name=spec.name,
                src=src,
                dst=dst,
                size=peer_bytes,
                send_parents=initial_frontier.get(src, []),
                recv_parents=initial_frontier.get(dst, []),
                op_name=f"{spec.name}_REP{repetition}_DIRECT",
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
    frontier = ctx.dependency_frontier(spec)

    # Repetitions of the same collective are sequential. Independent
    # collectives remain concurrent unless depends_on says otherwise.
    for repetition in range(spec.repetitions):
        frontier = expander(
            ctx,
            spec,
            frontier,
            repetition,
        )

    ctx.terminals[spec.name] = {
        rank: list(frontier.get(rank, []))
        for rank in spec.ranks
    }


def load_spec(path: Path) -> Tuple[int, List[CollectiveSpec]]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if "num_ranks" not in raw:
        raise ValueError("workload_spec.json must contain 'num_ranks'")
    if "collectives" not in raw:
        raise ValueError("workload_spec.json must contain 'collectives'")

    num_ranks = int(raw["num_ranks"])
    if num_ranks <= 0:
        raise ValueError("num_ranks must be > 0")

    specs = [
        parse_collective(item, num_ranks)
        for item in raw["collectives"]
    ]
    if not specs:
        raise ValueError("collectives must not be empty")

    return num_ranks, topological_collective_order(specs)


def write_comm_group_metadata(
    output_dir: Path,
    specs: Sequence[CollectiveSpec],
) -> None:
    """
    Emit communicator metadata.

    Explicit P2P ET nodes do not consume pg_name, so this file is not required
    for correctness. It is emitted to preserve the experiment's communicator
    description and to keep existing ASTRA-sim command lines usable.
    """
    groups = {
        str(index): list(spec.ranks)
        for index, spec in enumerate(specs)
    }

    with (output_dir / "comm_group.json").open("w", encoding="utf-8") as f:
        json.dump(groups, f, indent=2)
        f.write("\n")


def remove_old_et_files(output_dir: Path) -> None:
    for path in output_dir.glob("workload.*.et"):
        path.unlink()


def main() -> None:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Expand collective specs into explicit Chakra P2P ET files."
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=script_dir / "workload_spec.json",
        help="Workload specification JSON "
             "(default: workload_spec.json next to this script)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=script_dir / "workload",
        help="Output directory "
             "(default: workload/ next to this script)",
    )
    args = parser.parse_args()

    spec_path = args.spec.resolve()
    output_dir = args.output_dir.resolve()

    num_ranks, specs = load_spec(spec_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    remove_old_et_files(output_dir)

    ctx = ExpansionContext(num_ranks)

    for spec in specs:
        expand_collective(ctx, spec)

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
        flows = ctx.flow_count.get(spec.name, 0)
        net_bytes = ctx.network_bytes.get(spec.name, 0)

        print(
            f"[{spec.name}] "
            f"{spec.type}/{spec.algorithm} "
            f"ranks={list(spec.ranks)} "
            f"bytes={spec.bytes} "
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
        print()

    print("ASTRA workload prefix:")
    print(f"  {output_dir / 'workload'}")
    print()
    print("Communicator metadata:")
    print(f"  {output_dir / 'comm_group.json'}")


if __name__ == "__main__":
    main()
