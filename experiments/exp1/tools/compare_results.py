#!/usr/bin/env python3
"""Create a Markdown comparison for one exp1 four-case run."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


STAT_RE = re.compile(
    r"sys\[(?P<rank>\d+)\], (?P<kind>Wall|Comm) time: (?P<cycles>\d+)"
)
VERIFY_RE = re.compile(
    r"PATH VERIFY PASSED case=(?P<case>\w+) flows=(?P<flows>\d+) "
    r"cross_side_flows=(?P<cross>\d+) "
    r"forwarding_records=(?P<forwarding>\d+) "
    r"finished_ranks=(?P<finished>\d+)"
)
CASE_ORDER = {"path0": 0, "path1": 1, "mixed": 2, "reverse": 3}


@dataclass(frozen=True)
class Result:
    case: str
    directory: Path
    ar_path: str
    a2a_path: str
    wall: dict[int, int]
    comm: dict[int, int]
    flows: int
    cross_flows: int
    forwarding: int
    finished: int

    @property
    def makespan(self) -> int:
        return max(self.wall.values())

    @property
    def average_wall(self) -> float:
        return sum(self.wall.values()) / len(self.wall)

    @property
    def average_comm(self) -> float:
        return sum(self.comm.values()) / len(self.comm)


def path_label(value: object) -> str:
    if value is None:
        return "ECMP"
    path_id = int(value)
    return f"path {path_id} ({'short' if path_id == 0 else 'long'})"


def load_result(directory: Path) -> Result:
    resolved_path = directory / "config" / "workload_spec.resolved.json"
    log_path = directory / "run.log"
    if not resolved_path.is_file() or not log_path.is_file():
        raise ValueError(f"归档不完整：{directory}")

    resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
    collective_paths: dict[str, str] = {}
    for node in resolved["nodes"]:
        if node["type"] == "collective":
            collective_paths[node["collective"]] = path_label(node.get("path_id"))

    wall: dict[int, int] = {}
    comm: dict[int, int] = {}
    verification = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = STAT_RE.search(line)
        if match:
            target = wall if match.group("kind") == "Wall" else comm
            target[int(match.group("rank"))] = int(match.group("cycles"))
        match = VERIFY_RE.search(line)
        if match:
            verification = match.groupdict()

    if set(wall) != set(range(8)) or set(comm) != set(range(8)):
        raise ValueError(f"缺少 rank 0..7 的统计数据：{log_path}")
    if verification is None:
        raise ValueError(f"缺少 PATH VERIFY PASSED：{log_path}")

    case_name = verification["case"]
    return Result(
        case=case_name,
        directory=directory,
        ar_path=collective_paths.get("allreduce", "未配置"),
        a2a_path=collective_paths.get("alltoall", "未配置"),
        wall=wall,
        comm=comm,
        flows=int(verification["flows"]),
        cross_flows=int(verification["cross"]),
        forwarding=int(verification["forwarding"]),
        finished=int(verification["finished"]),
    )


def format_int(value: int | float) -> str:
    return f"{value:,.0f}"


def build_report(results: list[Result], output_path: Path) -> str:
    results.sort(key=lambda item: CASE_ORDER.get(item.case, 999))
    found = {item.case for item in results}
    expected = set(CASE_ORDER)
    if found != expected or len(results) != len(expected):
        raise ValueError(
            f"对比必须恰好包含 {sorted(expected)}，实际得到 {sorted(found)}"
        )

    best_makespan = min(item.makespan for item in results)
    lines = [
        "# exp1 四种路径配置结果对比",
        "",
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "## 总览",
        "",
        "| case | Ring AllReduce | Direct All-to-All | 完成周期（最大 Wall） | 相对最快 | 平均 Wall | 平均 Comm | flows | 跨侧 flows | forwarding | 完成 ranks |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        slowdown = (item.makespan / best_makespan - 1.0) * 100.0
        lines.append(
            f"| {item.case} | {item.ar_path} | {item.a2a_path} | "
            f"{format_int(item.makespan)} | +{slowdown:.2f}% | "
            f"{format_int(item.average_wall)} | {format_int(item.average_comm)} | "
            f"{item.flows} | {item.cross_flows} | {item.forwarding} | {item.finished}/8 |"
        )

    fastest = [item.case for item in results if item.makespan == best_makespan]
    lines.extend(
        [
            "",
            f"最快配置：`{', '.join(fastest)}`，完成周期为 `{format_int(best_makespan)}`。",
            "",
            "## 各 rank 的 Wall time（cycles）",
            "",
            "| rank | " + " | ".join(item.case for item in results) + " |",
            "|---:|" + "---:|" * len(results),
        ]
    )
    for rank in range(8):
        lines.append(
            f"| {rank} | "
            + " | ".join(format_int(item.wall[rank]) for item in results)
            + " |"
        )

    lines.extend(["", "## 本轮实验归档", ""])
    for item in results:
        relative = Path(item.directory.name)
        lines.append(f"- `{item.case}`：[{relative}]({relative}/)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("run_dirs", nargs=4, type=Path)
    args = parser.parse_args()

    results = [load_result(path.resolve()) for path in args.run_dirs]
    report = build_report(results, args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(report, encoding="utf-8")
    temporary.replace(args.output)

    print(f"Comparison report: {args.output}")
    for item in sorted(results, key=lambda result: CASE_ORDER[result.case]):
        print(f"  {item.case:7s} makespan={item.makespan}")


if __name__ == "__main__":
    main()
