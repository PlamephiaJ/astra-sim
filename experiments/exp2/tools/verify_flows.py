#!/usr/bin/env python3
"""Verify exp2's selectable flow-level ECMP and UGAL-L strategies."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


ASTRA_RE = re.compile(
    r"^ASTRA_ROUTE src=(?P<src>\d+) dst=(?P<dst>\d+) "
    r"tag=(?P<tag>\d+) bytes=(?P<bytes>\d+) "
    r"label=(?P<label>-?\d+) route=(?P<route>\w+) "
    r"flow=(?P<flow>\S+) sport=(?P<sport>\d+) dport=(?P<dport>\d+)"
)
NS3_ROUTE_RE = re.compile(
    r"^NS3_ROUTE switch=(?P<switch>\d+) "
    r"src_ip=(?P<src_ip>\S+) dst_ip=(?P<dst_ip>\S+) "
    r"sport=(?P<sport>\d+) dport=(?P<dport>\d+) "
    r"label=(?P<label>\d+) route=(?P<route>\w+) .* "
    r"next_hop=(?P<next_hop>\d+)"
)
NS3_ECMP_RE = re.compile(
    r"^NS3_ECMP switch=(?P<switch>\d+) "
    r"src_ip=(?P<src_ip>\S+) dst_ip=(?P<dst_ip>\S+) "
    r"sport=(?P<sport>\d+) dport=(?P<dport>\d+) "
    r"candidates=(?P<candidates>\d+) .* "
    r"next_hop=(?P<next_hop>\d+)"
)
NS3_UGAL_RE = re.compile(
    r"^NS3_UGAL switch=(?P<switch>\d+) "
    r"src_ip=(?P<src_ip>\S+) dst_ip=(?P<dst_ip>\S+) "
    r"sport=(?P<sport>\d+) dport=(?P<dport>\d+) "
    r"minimal_q_bytes=(?P<minimal_q>\d+) "
    r"nonminimal_q_bytes=(?P<nonminimal_q>\d+) "
    r"minimal_hops=(?P<minimal_hops>\d+) "
    r"nonminimal_hops=(?P<nonminimal_hops>\d+) "
    r"minimal_cost=(?P<minimal_cost>\d+) "
    r"nonminimal_cost=(?P<nonminimal_cost>\d+) "
    r"bias_bytes=(?P<bias>\d+) "
    r"decision=(?P<decision>minimal|nonminimal) .* "
    r"next_hop=(?P<next_hop>\d+)"
)
FINISH_RE = re.compile(r"\[workload\] \[info\] sys\[(?P<rank>\d+)\] finished,")
LABEL_TO_ROUTE = {-1: "default", 0: "short", 1: "long"}


def rank_from_ip(value: str) -> int:
    packed = int(ipaddress.ip_address(value))
    return (packed >> 8) & 0xFFFF


def expected_label(item: dict) -> int:
    value = item.get("routing_label")
    return -1 if value is None else int(value)


def ports_key(fields: dict[str, str]) -> tuple[int, int, int, int]:
    return (
        rank_from_ip(fields["src_ip"]),
        rank_from_ip(fields["dst_ip"]),
        int(fields["sport"]),
        int(fields["dport"]),
    )


def lookup_astra_label(
    key: tuple[int, int, int, int],
    port_labels: dict[tuple[int, int, int, int], int],
) -> int | None:
    label = port_labels.get(key)
    if label is not None:
        return label
    src, dst, sport, dport = key
    return port_labels.get((dst, src, dport, sport))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("ecmp", "ugal_l"), required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--flow-plan", type=Path, required=True)
    args = parser.parse_args()

    plan = json.loads(args.flow_plan.read_text(encoding="utf-8"))["flows"]
    expected_by_id = {item["flow_id"]: item for item in plan}
    errors: list[str] = []
    if len(expected_by_id) != len(plan):
        errors.append("flow_plan contains duplicate flow_id values")

    observed_ids: set[str] = set()
    label_counts: dict[str, Counter[int]] = defaultdict(Counter)
    port_labels: dict[tuple[int, int, int, int], int] = {}
    finished_ranks: set[int] = set()
    next_hops: Counter[int] = Counter()
    inter_switch_hops: Counter[int] = Counter()
    ugal_decisions: Counter[str] = Counter()
    pinned_records = 0
    ecmp_records = 0
    ugal_records = 0
    switch10_records = 0
    cross_side_count = 0

    for line_number, line in enumerate(
        args.log.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        match = ASTRA_RE.match(line)
        if match:
            fields = match.groupdict()
            flow_id = fields["flow"]
            label = int(fields["label"])
            src = int(fields["src"])
            dst = int(fields["dst"])
            expected = expected_by_id.get(flow_id)
            if expected is None:
                errors.append(f"line {line_number}: unknown flow_id {flow_id}")
                continue
            if flow_id in observed_ids:
                errors.append(f"line {line_number}: duplicate ASTRA flow {flow_id}")
            observed_ids.add(flow_id)
            for key, actual in (("src", src), ("dst", dst), ("tag", int(fields["tag"]))):
                if actual != int(expected[key]):
                    errors.append(
                        f"line {line_number}: {flow_id} {key}={actual}, "
                        f"expected {expected[key]}"
                    )
            planned_label = expected_label(expected)
            if label != planned_label or fields["route"] != LABEL_TO_ROUTE[label]:
                errors.append(
                    f"line {line_number}: {flow_id} label={label}/{fields['route']}, "
                    f"expected {planned_label}/{LABEL_TO_ROUTE[planned_label]}"
                )
            label_counts[str(expected["collective"])][label] += 1
            port_labels[(src, dst, int(fields["sport"]), int(fields["dport"]))] = label
            if src // 4 != dst // 4:
                cross_side_count += 1
            continue

        match = FINISH_RE.search(line)
        if match:
            finished_ranks.add(int(match.group("rank")))
            continue

        match = NS3_ROUTE_RE.match(line)
        if match:
            fields = match.groupdict()
            label = lookup_astra_label(ports_key(fields), port_labels)
            if label is None:
                errors.append(f"line {line_number}: NS3_ROUTE has no ASTRA flow")
                continue
            actual_label = int(fields["label"])
            next_hop = int(fields["next_hop"])
            pinned_records += 1
            if label != actual_label or next_hop != 10 + actual_label:
                errors.append(
                    f"line {line_number}: pinned route label/hop "
                    f"{actual_label}/{next_hop}, expected {label}/{10 + label}"
                )
            continue

        match = NS3_UGAL_RE.match(line)
        if match:
            fields = match.groupdict()
            label = lookup_astra_label(ports_key(fields), port_labels)
            if label is None:
                errors.append(f"line {line_number}: NS3_UGAL has no ASTRA flow")
                continue

            switch = int(fields["switch"])
            next_hop = int(fields["next_hop"])
            minimal_q = int(fields["minimal_q"])
            nonminimal_q = int(fields["nonminimal_q"])
            minimal_hops = int(fields["minimal_hops"])
            nonminimal_hops = int(fields["nonminimal_hops"])
            minimal_cost = int(fields["minimal_cost"])
            nonminimal_cost = int(fields["nonminimal_cost"])
            bias = int(fields["bias"])
            decision = fields["decision"]
            ugal_records += 1
            ugal_decisions[decision] += 1

            if label != -1:
                errors.append(
                    f"line {line_number}: labeled flow unexpectedly used UGAL-L"
                )
            if switch not in (8, 9) or minimal_hops != 1 or nonminimal_hops != 2:
                errors.append(
                    f"line {line_number}: invalid UGAL-L topology metadata"
                )
            if minimal_cost != minimal_q * minimal_hops:
                errors.append(
                    f"line {line_number}: invalid UGAL-L minimal cost"
                )
            if nonminimal_cost != nonminimal_q * nonminimal_hops + bias:
                errors.append(
                    f"line {line_number}: invalid UGAL-L non-minimal cost"
                )
            expected_decision = (
                "nonminimal" if nonminimal_cost < minimal_cost else "minimal"
            )
            expected_next_hop = (
                10 if expected_decision == "nonminimal" else (9 if switch == 8 else 8)
            )
            if decision != expected_decision or next_hop != expected_next_hop:
                errors.append(
                    f"line {line_number}: UGAL-L chose {decision}/{next_hop}, "
                    f"expected {expected_decision}/{expected_next_hop}"
                )
            continue

        match = NS3_ECMP_RE.match(line)
        if match:
            fields = match.groupdict()
            label = lookup_astra_label(ports_key(fields), port_labels)
            if label is None:
                errors.append(f"line {line_number}: NS3_ECMP has no ASTRA flow")
                continue
            next_hop = int(fields["next_hop"])
            candidates = int(fields["candidates"])
            ecmp_records += 1
            next_hops[next_hop] += 1
            if label != -1:
                errors.append(
                    f"line {line_number}: labeled flow unexpectedly used ECMP"
                )
            switch = int(fields["switch"])
            if switch == 10:
                switch10_records += 1
            if switch in (8, 9) and next_hop in (8, 9):
                inter_switch_hops[next_hop] += 1
                if candidates != 1:
                    errors.append(
                        f"line {line_number}: direct shortest path has "
                        f"{candidates} candidates, expected 1"
                    )
            if next_hop == 10:
                errors.append(
                    f"line {line_number}: standard ECMP selected two-hop detour via 10"
                )

    missing_ids = set(expected_by_id) - observed_ids
    if missing_ids:
        errors.append(f"missing {len(missing_ids)} ASTRA flows from flow_plan")
    if len(plan) != 36:
        errors.append(f"flow_plan has {len(plan)} flows, expected 36")
    if cross_side_count != 20:
        errors.append(f"observed {cross_side_count} cross-side flows, expected 20")
    if finished_ranks != set(range(8)):
        errors.append(f"finished ranks are {sorted(finished_ranks)}, expected 0..7")

    combined_labels = sum(label_counts.values(), Counter())
    expected_case_label = -1
    if combined_labels != Counter({expected_case_label: 36}):
        errors.append(
            f"ASTRA label counts are {dict(combined_labels)}, "
            f"expected {{{expected_case_label}: 36}}"
        )
    if pinned_records != 0:
        errors.append(
            f"{args.case} produced {pinned_records} pinned forwarding records"
        )

    if args.case == "ecmp":
        if ugal_records != 0:
            errors.append(f"ECMP produced {ugal_records} UGAL-L decisions")
        if ecmp_records != 112:
            errors.append(
                f"ECMP forwarding records={ecmp_records}, expected 112"
            )
        expected_inter_switch = Counter({8: 20, 9: 20})
        if inter_switch_hops != expected_inter_switch:
            errors.append(
                f"inter-switch next hops are {dict(inter_switch_hops)}, "
                f"expected {dict(expected_inter_switch)}"
            )
        if switch10_records != 0:
            errors.append(
                f"ECMP traversed non-minimal switch 10 {switch10_records} times"
            )
    else:
        if ugal_records != 40:
            errors.append(
                f"UGAL-L decisions={ugal_records}, expected 40"
            )
        if sum(ugal_decisions.values()) != 40:
            errors.append(
                f"UGAL-L decision counts are {dict(ugal_decisions)}, expected 40"
            )
        nonminimal_count = ugal_decisions["nonminimal"]
        if switch10_records != nonminimal_count:
            errors.append(
                f"switch 10 traversals={switch10_records}, "
                f"non-minimal decisions={nonminimal_count}"
            )
        if ecmp_records != 72 + nonminimal_count:
            errors.append(
                f"downstream ECMP records={ecmp_records}, "
                f"expected {72 + nonminimal_count}"
            )

    if errors:
        raise SystemExit("FLOW ROUTE VERIFY FAILED\n  " + "\n  ".join(errors[:30]))

    inter_switch_text = "/".join(
        f"n{hop}={count}" for hop, count in sorted(inter_switch_hops.items())
    )
    ugal_text = "/".join(
        f"{decision}={count}"
        for decision, count in sorted(ugal_decisions.items())
    )
    print(
        f"FLOW ROUTE VERIFY PASSED case={args.case} flows={len(observed_ids)} "
        f"cross_side_flows={cross_side_count} pinned_records={pinned_records} "
        f"ecmp_records={ecmp_records} ugal_records={ugal_records} "
        f"ugal_decisions={ugal_text or 'none'} switch10_records={switch10_records} "
        f"inter_switch_records="
        f"{sum(inter_switch_hops.values())} finished_ranks={len(finished_ranks)} "
        f"inter_switch_next_hops={inter_switch_text or 'none'}"
    )


if __name__ == "__main__":
    main()
