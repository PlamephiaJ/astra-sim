#!/usr/bin/env python3
"""Plot an exp1 cycle sweep by parsing each point's comparison.md.

The plotter intentionally uses only the Python standard library.  It writes an
SVG (for reports and browsers) and a CSV containing the extracted values.
"""

from __future__ import annotations

import argparse
import csv
import html
import math
import re
from dataclasses import dataclass
from pathlib import Path


CASES = ("path0", "path1", "mixed", "reverse")
COLORS = {
    "path0": "#0072B2",
    "path1": "#D55E00",
    "mixed": "#009E73",
    "reverse": "#CC79A7",
}


@dataclass(frozen=True)
class Point:
    cycles: int
    values: dict[str, int]
    source: Path


def parse_comparison(path: Path) -> dict[str, int]:
    """Extract the maximum Wall-time column from one Markdown summary."""
    rows = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    header_index = next(
        (
            index
            for index, line in enumerate(rows)
            if line.startswith("| case |") and "完成周期" in line
        ),
        None,
    )
    if header_index is None:
        raise ValueError(f"Cannot find the overview table in {path}")

    headers = [cell.strip() for cell in rows[header_index].strip("|").split("|")]
    wall_index = next(
        (index for index, name in enumerate(headers) if name.startswith("完成周期")),
        None,
    )
    if wall_index is None:
        raise ValueError(f"Cannot find the maximum Wall-time column in {path}")

    values: dict[str, int] = {}
    for line in rows[header_index + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and cells[0] in CASES:
            values[cells[0]] = int(cells[wall_index].replace(",", ""))

    missing = set(CASES) - values.keys()
    if missing:
        raise ValueError(f"Missing cases {sorted(missing)} in {path}")
    return values


def load_points(root: Path) -> list[Point]:
    points: list[Point] = []
    for path in root.glob("cycles_*/comparison.md"):
        match = re.fullmatch(r"cycles_(\d+)", path.parent.name)
        if match:
            points.append(Point(int(match.group(1)), parse_comparison(path), path))
    points.sort(key=lambda point: point.cycles)
    if not points:
        raise ValueError(f"No cycles_*/comparison.md files found below {root}")
    if len({point.cycles for point in points}) != len(points):
        raise ValueError("Duplicate cycle values found")
    return points


def write_csv(points: list[Point], output: Path, root: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("compute_cycles", *CASES, "fastest", "source"))
        for point in points:
            best = min(point.values.values())
            fastest = "+".join(case for case in CASES if point.values[case] == best)
            writer.writerow(
                (
                    point.cycles,
                    *(point.values[case] for case in CASES),
                    fastest,
                    point.source.relative_to(root),
                )
            )
    temporary.replace(output)


def nice_step(span: float, target_ticks: int = 6) -> float:
    raw = span / target_ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    normalized = raw / magnitude
    factor = 1 if normalized <= 1 else 2 if normalized <= 2 else 5 if normalized <= 5 else 10
    return factor * magnitude


def svg_text(x: float, y: float, value: str, **attrs: object) -> str:
    attributes = " ".join(f'{("class" if key == "class_" else key.replace("_", "-"))}="{html.escape(str(val))}"' for key, val in attrs.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" {attributes}>{html.escape(value)}</text>'


def write_svg(points: list[Point], output: Path) -> None:
    width, height = 1500, 900
    left, right, top, bottom = 125, 55, 105, 115
    plot_width = width - left - right
    plot_height = height - top - bottom
    x_min, x_max = points[0].cycles, points[-1].cycles
    raw_min = min(value for point in points for value in point.values.values())
    raw_max = max(value for point in points for value in point.values.values())
    y_step = nice_step(raw_max - raw_min)
    y_min = math.floor(raw_min / y_step) * y_step
    y_max = math.ceil(raw_max / y_step) * y_step

    def sx(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min or 1) * plot_width

    def sy(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min or 1) * plot_height

    winners = []
    for point in points:
        best = min(point.values.values())
        winners.append("+".join(case for case in CASES if point.values[case] == best))

    winner_runs: list[tuple[int, int, str]] = []
    start = 0
    for index in range(1, len(points) + 1):
        if index == len(points) or winners[index] != winners[start]:
            winner_runs.append((start, index - 1, winners[start]))
            start = index

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text { font-family: Inter, DejaVu Sans, Arial, sans-serif; fill: #202124; }",
        ".grid { stroke: #d9dee5; stroke-width: 1; }",
        ".axis { stroke: #343a40; stroke-width: 1.6; }",
        ".tick { font-size: 17px; fill: #50565e; }",
        ".label { font-size: 21px; font-weight: 600; }",
        ".legend { font-size: 19px; font-weight: 600; }",
        "</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        svg_text(width / 2, 42, "exp1 routing comparison across compute cycles", text_anchor="middle", font_size="27", font_weight="700"),
        svg_text(width / 2, 72, "Maximum rank Wall time from each comparison.md (lower is better)", text_anchor="middle", font_size="18", fill="#59636e"),
    ]

    # Lightly shade regions according to the winning case.
    for start_index, end_index, winner in winner_runs:
        x0 = left if start_index == 0 else (sx(points[start_index - 1].cycles) + sx(points[start_index].cycles)) / 2
        x1 = left + plot_width if end_index == len(points) - 1 else (sx(points[end_index].cycles) + sx(points[end_index + 1].cycles)) / 2
        color = COLORS.get(winner, "#777777")
        parts.append(f'<rect x="{x0:.1f}" y="{top}" width="{x1 - x0:.1f}" height="{plot_height}" fill="{color}" opacity="0.055"/>')
        if x1 - x0 > 90:
            parts.append(svg_text((x0 + x1) / 2, top + 25, f"fastest: {winner}", text_anchor="middle", font_size="16", font_weight="600", fill=color))

    y_tick = y_min
    while y_tick <= y_max + 0.5:
        y = sy(y_tick)
        parts.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}"/>')
        parts.append(svg_text(left - 14, y + 6, f"{y_tick / 1_000_000:g}", text_anchor="end", class_="tick"))
        y_tick += y_step

    x_step = nice_step(x_max - x_min, 7)
    x_tick = math.ceil(x_min / x_step) * x_step
    while x_tick <= x_max + 0.5:
        x = sx(x_tick)
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}"/>')
        parts.append(svg_text(x, top + plot_height + 31, f"{int(x_tick):,}", text_anchor="middle", class_="tick"))
        x_tick += x_step

    parts.extend(
        (
            f'<line class="axis" x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}"/>',
            f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}"/>',
            svg_text(left + plot_width / 2, height - 45, "Compute duration (cycles)", text_anchor="middle", class_="label"),
            f'<text x="31" y="{top + plot_height / 2:.1f}" text-anchor="middle" class="label" transform="rotate(-90 31 {top + plot_height / 2:.1f})">Completion time (million cycles)</text>',
        )
    )

    for case in CASES:
        coordinates = " ".join(f"{sx(point.cycles):.1f},{sy(point.values[case]):.1f}" for point in points)
        parts.append(f'<polyline points="{coordinates}" fill="none" stroke="{COLORS[case]}" stroke-width="4" stroke-linejoin="round" stroke-linecap="round"/>')
        for point in points:
            parts.append(f'<circle cx="{sx(point.cycles):.1f}" cy="{sy(point.values[case]):.1f}" r="2.7" fill="{COLORS[case]}"/>')

    legend_width = 510
    legend_x = left + plot_width - legend_width - 15
    legend_y = top + 45
    parts.append(f'<rect x="{legend_x}" y="{legend_y}" width="{legend_width}" height="62" rx="8" fill="#ffffff" stroke="#cbd2da" opacity="0.96"/>')
    for index, case in enumerate(CASES):
        x = legend_x + 22 + index * 125
        y = legend_y + 32
        parts.append(f'<line x1="{x}" y1="{y}" x2="{x + 28}" y2="{y}" stroke="{COLORS[case]}" stroke-width="4"/>')
        parts.append(svg_text(x + 36, y + 6, case, class_="legend"))

    parts.append(svg_text(left, height - 15, f"{len(points)} sweep points; y-axis starts at {y_min / 1_000_000:g}M cycles.", font_size="15", fill="#6b737c"))
    parts.append("</svg>")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text("\n".join(parts) + "\n", encoding="utf-8")
    temporary.replace(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path, help="Sweep directory containing cycles_*/comparison.md")
    parser.add_argument("--svg", type=Path, help="SVG output (default: ROOT/sweep_comparison.svg)")
    parser.add_argument("--csv", type=Path, help="CSV output (default: ROOT/sweep_comparison.csv)")
    args = parser.parse_args()

    root = args.root.resolve()
    svg_output = (args.svg or root / "sweep_comparison.svg").resolve()
    csv_output = (args.csv or root / "sweep_comparison.csv").resolve()
    points = load_points(root)
    write_csv(points, csv_output, root)
    write_svg(points, svg_output)
    print(f"Parsed {len(points)} sweep points")
    print(f"CSV: {csv_output}")
    print(f"SVG: {svg_output}")


if __name__ == "__main__":
    main()
