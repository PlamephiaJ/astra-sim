#!/usr/bin/env python3
"""Summarize exp1 path-case results across compute-duration sweep points."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from compare_results import CASE_ORDER, Result, load_result


CASES = tuple(sorted(CASE_ORDER, key=CASE_ORDER.get))


def load_duration_us(cycle_dir: Path) -> int:
    resolved_path = cycle_dir / "mixed" / "config" / "workload_spec.resolved.json"
    resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
    values = {
        int(node["cycles"])
        for node in resolved["nodes"]
        if node["type"] == "compute"
    }
    if len(values) != 1:
        raise ValueError(
            f"Expected one common compute duration in {resolved_path}, got {values}"
        )
    return values.pop()


def load_sweep_point(cycle_dir: Path) -> tuple[int, dict[str, Result]]:
    results = {
        case: load_result(cycle_dir / case)
        for case in CASES
    }
    return load_duration_us(cycle_dir), results


def build_report(
    points: list[tuple[int, Path, dict[str, Result]]],
    output_path: Path,
) -> str:
    points.sort(key=lambda item: item[0])
    lines = [
        "# exp1 compute-duration sweep",
        "",
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "每个 sweep 点内部的四种 routing case 并行执行。配置时长单位为微秒；结果为最大 Wall time（simulator cycles）。",
        "",
        "| compute duration (us) | path0 | path1 | mixed | reverse | fastest | detail |",
        "|---:|---:|---:|---:|---:|---|---|",
    ]
    for duration_us, cycle_dir, results in points:
        best = min(result.makespan for result in results.values())
        fastest = ", ".join(
            case for case in CASES if results[case].makespan == best
        )
        relative = Path(os.path.relpath(cycle_dir, output_path.parent))
        lines.append(
            f"| {duration_us:,} | {results['path0'].makespan:,} | "
            f"{results['path1'].makespan:,} | {results['mixed'].makespan:,} | "
            f"{results['reverse'].makespan:,} | {fastest} | "
            f"[comparison]({relative}/comparison.md) |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("cycle_dirs", nargs="+", type=Path)
    args = parser.parse_args()

    output_path = args.output.resolve()
    points = []
    for directory in args.cycle_dirs:
        cycle_dir = directory.resolve()
        duration_us, results = load_sweep_point(cycle_dir)
        points.append((duration_us, cycle_dir, results))

    report = build_report(points, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_text(report, encoding="utf-8")
    temporary.replace(output_path)
    print(f"Sweep report: {output_path}")


if __name__ == "__main__":
    main()
