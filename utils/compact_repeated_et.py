#!/usr/bin/env python3
"""Extract the base pattern from expanded Chakra ET files.

STAGE's microbatch post-process encodes replica ``r`` as
``base_node_id + r * id_offset``.  This tool keeps replica zero byte-for-byte
and writes a small ``.repeat`` sidecar consumed by the experimental feeder.
It parses only protobuf framing and the Node.id wire field, so it does not
require generated Python protobuf bindings.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import BinaryIO


def format_duration(seconds: float) -> str:
    if seconds == float("inf"):
        return "--:--:--"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class Progress:
    def __init__(self, total_bytes: int, total_ranks: int) -> None:
        self.total_bytes = total_bytes
        self.total_ranks = total_ranks
        self.completed_bytes = 0
        self.start = time.monotonic()
        self.last_render = 0.0
        self.interactive = sys.stdout.isatty()

    def advance(self, byte_count: int, rank: int, *, force: bool = False) -> None:
        self.completed_bytes += byte_count
        now = time.monotonic()
        interval = 0.5 if self.interactive else 30.0
        if not force and now - self.last_render < interval:
            return
        self.last_render = now
        elapsed = max(now - self.start, 1e-9)
        fraction = min(self.completed_bytes / self.total_bytes, 1.0)
        rate = self.completed_bytes / elapsed
        eta = (self.total_bytes - self.completed_bytes) / rate if rate else float("inf")
        width = 30
        filled = int(width * fraction)
        bar = "#" * filled + "-" * (width - filled)
        message = (
            f"[{bar}] {fraction:6.2%} "
            f"rank {rank + 1}/{self.total_ranks} "
            f"{self.completed_bytes / (1024**3):.1f}/{self.total_bytes / (1024**3):.1f} GiB "
            f"{rate / (1024**2):.1f} MiB/s "
            f"elapsed {format_duration(elapsed)} ETA {format_duration(eta)}"
        )
        print(message, end="\r" if self.interactive else "\n", flush=True)

    def finish(self) -> None:
        if self.interactive:
            print()


def read_varint(stream: BinaryIO) -> tuple[int, bytes] | None:
    value = 0
    shift = 0
    encoded = bytearray()
    while shift < 64:
        byte = stream.read(1)
        if not byte:
            if not encoded:
                return None
            raise ValueError("truncated varint")
        encoded.extend(byte)
        value |= (byte[0] & 0x7F) << shift
        if byte[0] < 0x80:
            return value, bytes(encoded)
        shift += 7
    raise ValueError("varint exceeds 64 bits")


def payload_varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while shift < 64 and offset < len(payload):
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def node_id(payload: bytes) -> int:
    offset = 0
    while offset < len(payload):
        tag, offset = payload_varint(payload, offset)
        field = tag >> 3
        wire = tag & 0x7
        if wire == 0:
            value, offset = payload_varint(payload, offset)
            if field == 1:
                return value
        elif wire == 1:
            offset += 8
        elif wire == 2:
            size, offset = payload_varint(payload, offset)
            offset += size
        elif wire == 5:
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
    raise ValueError("Chakra Node message has no id field")


def compact_file(
    source: Path,
    destination: Path,
    repeat_count: int,
    id_offset: int,
    progress: Progress,
    rank: int,
) -> tuple[int, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    total_nodes = 0
    base_nodes = 0

    with source.open("rb") as src, temporary.open("wb") as dst:
        metadata_frame = read_varint(src)
        if metadata_frame is None:
            raise ValueError(f"empty ET file: {source}")
        metadata_size, metadata_prefix = metadata_frame
        metadata = src.read(metadata_size)
        if len(metadata) != metadata_size:
            raise ValueError(f"truncated metadata: {source}")
        dst.write(metadata_prefix)
        dst.write(metadata)
        pending_progress_bytes = len(metadata_prefix) + len(metadata)

        while True:
            frame = read_varint(src)
            if frame is None:
                break
            size, prefix = frame
            payload = src.read(size)
            if len(payload) != size:
                raise ValueError(f"truncated node in {source}")
            current_id = node_id(payload)
            if current_id >= repeat_count * id_offset:
                raise ValueError(
                    f"node {current_id} exceeds repeat range in {source}"
                )
            total_nodes += 1
            pending_progress_bytes += len(prefix) + len(payload)
            if current_id < id_offset:
                dst.write(prefix)
                dst.write(payload)
                base_nodes += 1
            if total_nodes % 10000 == 0:
                progress.advance(pending_progress_bytes, rank)
                pending_progress_bytes = 0

        progress.advance(pending_progress_bytes, rank, force=True)

    os.replace(temporary, destination)
    destination.with_name(destination.name + ".repeat").write_text(
        f"{repeat_count} {id_offset}\n", encoding="ascii"
    )
    return total_nodes, base_nodes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-prefix", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--ranks", type=int, required=True)
    parser.add_argument("--repeat-count", type=int, required=True)
    parser.add_argument("--id-offset", type=int, default=1_000_000_000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.ranks <= 0 or args.repeat_count <= 0 or args.id_offset <= 0:
        raise SystemExit("ranks, repeat-count, and id-offset must be positive")
    sources = [Path(f"{args.input_prefix}.{rank}.et") for rank in range(args.ranks)]
    missing = [str(source) for source in sources if not source.is_file()]
    if missing:
        raise SystemExit(f"missing input ET: {missing[0]}")
    progress = Progress(sum(source.stat().st_size for source in sources), args.ranks)
    total_nodes = 0
    total_base_nodes = 0
    for rank, source in enumerate(sources):
        destination = Path(f"{args.output_prefix}.{rank}.et")
        total, base = compact_file(
            source,
            destination,
            args.repeat_count,
            args.id_offset,
            progress,
            rank,
        )
        total_nodes += total
        total_base_nodes += base
    progress.finish()
    print(
        f"Done: {total_nodes} expanded nodes -> {total_base_nodes} base nodes "
        f"in {format_duration(time.monotonic() - progress.start)}"
    )


if __name__ == "__main__":
    main()
