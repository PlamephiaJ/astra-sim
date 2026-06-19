#!/usr/bin/env python3
"""Generate canonical Dragonfly topologies for the ASTRA-sim NS-3 backend."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path


def rank_id(group: int, router: int, terminal: int, groups: int, routers: int) -> int:
    """Map (group, router, terminal) to STAGE's dp-fastest numeric rank."""
    return group + groups * (router + routers * terminal)


def router_id(group: int, router: int, terminal_count: int, groups: int, routers: int) -> int:
    return terminal_count + group * routers + router


def peer_index(group: int, peer_group: int) -> int:
    """Return peer_group's dense index in [0, groups-2] for this group."""
    return peer_group if peer_group < group else peer_group - 1


def generate(
    global_links_per_router: int,
    routers_per_group: int,
    terminals_per_router: int,
    groups: int,
    terminal_rate: str,
    local_rate: str,
    global_rate: str,
    terminal_delay: str,
    local_delay: str,
    global_delay: str,
) -> tuple[list[int], list[tuple[int, int, str, str, float]]]:
    expected_groups = routers_per_group * global_links_per_router + 1
    if groups != expected_groups:
        raise ValueError(
            "canonical Dragonfly requires groups = routers_per_group * "
            f"global_links_per_router + 1 ({expected_groups}), got {groups}"
        )

    terminals = groups * routers_per_group * terminals_per_router
    switches = [
        router_id(group, router, terminals, groups, routers_per_group)
        for group in range(groups)
        for router in range(routers_per_group)
    ]
    links: list[tuple[int, int, str, str, float]] = []

    # Terminal links. Numeric ranks follow (dp=group, tp=router, pp=terminal),
    # matching datasets/stage/generate_llama3.sh.
    for group in range(groups):
        for router in range(routers_per_group):
            rid = router_id(group, router, terminals, groups, routers_per_group)
            for terminal in range(terminals_per_router):
                rank = rank_id(group, router, terminal, groups, routers_per_group)
                links.append((rank, rid, terminal_rate, terminal_delay, 0.0))

    # Complete graph inside every group.
    for group in range(groups):
        for left in range(routers_per_group):
            for right in range(left + 1, routers_per_group):
                links.append(
                    (
                        router_id(group, left, terminals, groups, routers_per_group),
                        router_id(group, right, terminals, groups, routers_per_group),
                        local_rate,
                        local_delay,
                        0.0,
                    )
                )

    # Exactly one global link per group pair. For each group, its 32 peer groups
    # are distributed four per router, giving every router exactly h links.
    for left_group in range(groups):
        for right_group in range(left_group + 1, groups):
            left_slot = peer_index(left_group, right_group)
            right_slot = peer_index(right_group, left_group)
            left_router = left_slot // global_links_per_router
            right_router = right_slot // global_links_per_router
            links.append(
                (
                    router_id(
                        left_group, left_router, terminals, groups, routers_per_group
                    ),
                    router_id(
                        right_group, right_router, terminals, groups, routers_per_group
                    ),
                    global_rate,
                    global_delay,
                    0.0,
                )
            )

    return switches, links


def validate(
    switches: list[int],
    links: list[tuple[int, int, str, str, float]],
    global_links_per_router: int,
    routers_per_group: int,
    terminals_per_router: int,
    groups: int,
) -> None:
    terminals = groups * routers_per_group * terminals_per_router
    switch_set = set(switches)
    degree = Counter[int]()
    terminal_degree = Counter[int]()
    global_degree = Counter[int]()
    local_degree = Counter[int]()

    for src, dst, _rate, delay, _error in links:
        degree[src] += 1
        degree[dst] += 1
        src_switch = src in switch_set
        dst_switch = dst in switch_set
        if src_switch != dst_switch:
            terminal = dst if src_switch else src
            terminal_degree[terminal] += 1
        elif src_switch and dst_switch:
            src_group = (src - terminals) // routers_per_group
            dst_group = (dst - terminals) // routers_per_group
            target = local_degree if src_group == dst_group else global_degree
            target[src] += 1
            target[dst] += 1
        else:
            raise ValueError(f"terminal-to-terminal link is not allowed: {src} {dst}")

    expected_router_degree = (
        terminals_per_router + (routers_per_group - 1) + global_links_per_router
    )
    assert switches == list(range(terminals, terminals + groups * routers_per_group))
    assert set(terminal_degree) == set(range(terminals))
    assert all(terminal_degree[node] == 1 for node in range(terminals))
    assert all(degree[node] == expected_router_degree for node in switches)
    assert all(local_degree[node] == routers_per_group - 1 for node in switches)
    assert all(global_degree[node] == global_links_per_router for node in switches)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--global-links-per-router", "-hpr", type=int, default=4)
    parser.add_argument("--routers-per-group", "-a", type=int, default=8)
    parser.add_argument("--terminals-per-router", "-p", type=int, default=4)
    parser.add_argument("--groups", "-g", type=int, default=33)
    parser.add_argument("--terminal-rate", default="400Gbps")
    parser.add_argument("--local-rate", default="400Gbps")
    parser.add_argument("--global-rate", default="400Gbps")
    parser.add_argument("--terminal-delay", default="0.0005ms")
    parser.add_argument("--local-delay", default="0.0005ms")
    parser.add_argument("--global-delay", default="0.005ms")
    args = parser.parse_args()

    switches, links = generate(
        args.global_links_per_router,
        args.routers_per_group,
        args.terminals_per_router,
        args.groups,
        args.terminal_rate,
        args.local_rate,
        args.global_rate,
        args.terminal_delay,
        args.local_delay,
        args.global_delay,
    )
    validate(
        switches,
        links,
        args.global_links_per_router,
        args.routers_per_group,
        args.terminals_per_router,
        args.groups,
    )

    terminals = args.groups * args.routers_per_group * args.terminals_per_router
    node_count = terminals + len(switches)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        output.write(f"{node_count} {len(switches)} {len(links)}\n")
        output.write(" ".join(map(str, switches)) + "\n")
        for src, dst, rate, delay, error in links:
            output.write(f"{src} {dst} {rate} {delay} {error:g}\n")

    print(
        f"generated {args.output}: {terminals} terminals, {len(switches)} "
        f"routers, {len(links)} links"
    )


if __name__ == "__main__":
    main()
