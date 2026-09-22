#!/usr/bin/env python3
"""Verify exp1's flow-level path-pinning oracle from its concise debug log."""

from __future__ import annotations

import argparse
import ipaddress
import re
from pathlib import Path


ASTRA_RE = re.compile(
    r"^ASTRA_PATH src=(?P<src>\d+) dst=(?P<dst>\d+) .* path=(?P<path>-?\d+)"
)
NS3_RE = re.compile(
    r"^NS3_PATH switch=(?P<switch>\d+) "
    r"src_ip=(?P<src_ip>\S+) dst_ip=(?P<dst_ip>\S+) .* "
    r"path=(?P<path>\d+) .* next_hop=(?P<next_hop>\d+)"
)
FINISH_RE = re.compile(r"\[workload\] \[info\] sys\[(?P<rank>\d+)\] finished,")

RING = {0, 1, 4, 5}
ALL_TO_ALL = {2, 3, 6, 7}


def rank_from_ip(value: str) -> int:
    packed = int(ipaddress.ip_address(value))
    return (packed >> 8) & 0xFFFF


def expected_path(case: str, src: int, dst: int) -> int:
    if case == "ecmp":
        return -1
    if case == "path0":
        return 0
    if case == "path1":
        return 1
    endpoints = {src, dst}
    if endpoints <= RING:
        return 1 if case == "reverse" else 0
    if endpoints <= ALL_TO_ALL:
        return 0 if case == "reverse" else 1
    raise AssertionError(f"flow {src}->{dst} is outside both configured groups")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case",
        choices=("mixed", "reverse", "path0", "path1", "ecmp"),
        required=True,
    )
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()

    if not args.log.is_file():
        raise SystemExit(f"missing experiment log: {args.log}")

    astra_count = 0
    cross_side_count = 0
    ns3_count = 0
    errors: list[str] = []
    finished_ranks: set[int] = set()

    for line_number, line in enumerate(
        args.log.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        match = ASTRA_RE.match(line)
        if match:
            fields = {key: int(value) for key, value in match.groupdict().items()}
            expected = expected_path(args.case, fields["src"], fields["dst"])
            astra_count += 1
            if fields["src"] // 4 != fields["dst"] // 4:
                cross_side_count += 1
            if fields["path"] != expected:
                errors.append(
                    f"line {line_number}: ASTRA flow {fields['src']}->{fields['dst']} "
                    f"uses path {fields['path']}, expected {expected}"
                )
            continue

        match = FINISH_RE.search(line)
        if match:
            finished_ranks.add(int(match.group("rank")))
            continue

        match = NS3_RE.match(line)
        if match:
            fields = match.groupdict()
            src = rank_from_ip(fields["src_ip"])
            dst = rank_from_ip(fields["dst_ip"])
            path = int(fields["path"])
            next_hop = int(fields["next_hop"])
            expected = expected_path(args.case, src, dst)
            if expected == -1:
                errors.append(
                    f"line {line_number}: unpinned ECMP case emitted an explicit "
                    "NS3_PATH record"
                )
                continue
            expected_hop = 10 + expected
            ns3_count += 1
            if path != expected or next_hop != expected_hop:
                errors.append(
                    f"line {line_number}: switch {fields['switch']} flow {src}->{dst} "
                    f"uses path {path}/next-hop {next_hop}, expected "
                    f"path {expected}/next-hop {expected_hop}"
                )

    if astra_count == 0:
        errors.append("no ASTRA_PATH flow records found")
    if cross_side_count == 0:
        errors.append("no cross-side ASTRA flow records found")
    if args.case != "ecmp" and ns3_count == 0:
        errors.append("no NS3_PATH multipath forwarding records found")
    if finished_ranks != set(range(8)):
        errors.append(
            f"finished ranks are {sorted(finished_ranks)}, expected ranks 0..7"
        )

    if errors:
        raise SystemExit("PATH VERIFY FAILED\n  " + "\n  ".join(errors[:20]))

    print(
        f"PATH VERIFY PASSED case={args.case} flows={astra_count} "
        f"cross_side_flows={cross_side_count} forwarding_records={ns3_count} "
        f"finished_ranks={len(finished_ranks)}"
    )
    if args.case == "ecmp":
        print("Legacy ECMP remained active because no explicit path was encoded.")
    else:
        print("Flow pinning assertion remained valid for every forwarded packet.")


if __name__ == "__main__":
    main()
