#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

DEFAULT_INPUT = Path("dd_final.csv")
DEFAULT_OUTPUT = Path("dd_final_plot.png")
BLOCK_RE = re.compile(
    r"^(?P<mechanism>.+?)\s+\[(?P<block>[0-9.]+)(?P<unit>[KMG]?)\]$"
)
BLOCK_ORDER = {"K": 1, "M": 2, "G": 3, "T": 4}
OPERATION_STYLE = {
    "READ": {"color": "blue", "marker": "o"},
    "WRITE": {"color": "red", "marker": "s"},
}
MECHANISM_ORDER = ["Raw I/O", "LUKS", "eCryptfs", "fscrypt"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a PNG chart from dd_final.csv showing READ and WRITE throughput per mechanism."
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Input CSV file",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output PNG file",
    )
    parser.add_argument(
        "--title",
        default="READ and WRITE throughput vs block size",
        help="Plot title",
    )
    return parser.parse_args()


def parse_block_size(block_text: str) -> float:
    match = re.fullmatch(
        r"([0-9.]+)([KMGTP]?)", block_text.strip(), re.IGNORECASE
    )
    if not match:
        raise ValueError(f"Unsupported block size: {block_text}")

    value = float(match.group(1))
    unit = match.group(2).upper()
    factor = 1024 ** BLOCK_ORDER.get(unit, 0)
    return value * factor


def format_block_size(bytes_value: float) -> str:
    if bytes_value >= 1024**3 and bytes_value % (1024**3) == 0:
        return f"{int(bytes_value // (1024**3))}G"
    if bytes_value >= 1024**2 and bytes_value % (1024**2) == 0:
        return f"{int(bytes_value // (1024**2))}M"
    if bytes_value >= 1024 and bytes_value % 1024 == 0:
        return f"{int(bytes_value // 1024)}K"
    return str(int(bytes_value))


def mechanism_sort_key(name: str) -> tuple[int, str]:
    try:
        return (MECHANISM_ORDER.index(name), name)
    except ValueError:
        return (len(MECHANISM_ORDER), name)


def load_rows(
    csv_path: Path,
) -> dict[str, dict[str, list[tuple[float, float]]]]:
    grouped: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            operation = row["Operation"].strip().upper()
            if operation not in {"READ", "WRITE"}:
                continue

            speed_text = row["Speed (MBs)"].strip()
            if not speed_text or speed_text.upper() == "N/A":
                continue

            match = BLOCK_RE.match(row["Mechanism"].strip())
            if not match:
                raise ValueError(
                    f"Cannot parse mechanism label: {row['Mechanism']}"
                )

            mechanism = match.group("mechanism").strip()
            block_text = f"{match.group('block')}{match.group('unit').upper()}"
            block_bytes = parse_block_size(block_text)
            grouped[mechanism][operation].append(
                (block_bytes, float(speed_text))
            )

    return grouped


def prepare_axes_count(count: int) -> tuple[int, int]:
    cols = 2 if count > 1 else 1
    rows = math.ceil(count / cols)
    return rows, cols


def plot_data(
    data: dict[str, dict[str, list[tuple[float, float]]]],
    output_path: Path,
    title: str,
) -> None:
    mechanisms = sorted(data.keys(), key=mechanism_sort_key)
    rows, cols = prepare_axes_count(len(mechanisms))

    fig, axes = plt.subplots(
        rows, cols, figsize=(8 * cols, 5 * rows), sharex=True, sharey=True
    )
    axes_list = list(axes.flat) if hasattr(axes, "flat") else [axes]

    all_blocks = sorted(
        {
            block
            for mechanism_data in data.values()
            for values in mechanism_data.values()
            for block, _ in values
        }
    )
    x_ticks = all_blocks
    x_labels = [format_block_size(block) for block in x_ticks]

    for axis_index, mechanism in enumerate(mechanisms):
        ax = axes_list[axis_index]
        mechanism_data = data[mechanism]

        for operation in ("READ", "WRITE"):
            points = sorted(
                mechanism_data.get(operation, []), key=lambda item: item[0]
            )
            if not points:
                continue
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            style = OPERATION_STYLE[operation]
            ax.plot(
                xs,
                ys,
                label=operation,
                linewidth=2.2,
                markersize=7,
                color=style["color"],
                marker=style["marker"],
            )

        ax.set_title(mechanism)
        ax.set_xscale("log", base=2)
        ax.set_xticks(x_ticks)
        ax.set_xticklabels(x_labels)
        ax.tick_params(axis="x", labelbottom=True)
        ax.tick_params(axis="y", labelleft=True)
        ax.grid(True, which="both", linestyle="--", linewidth=0.6, alpha=0.5)
        ax.set_xlabel("Block size [B]")
        ax.set_ylabel("Speed [MB/s]")
        ax.legend(frameon=False)

    for axis_index in range(len(mechanisms), len(axes_list)):
        axes_list[axis_index].set_visible(False)

    fig.suptitle(title, fontsize=16, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    args = parse_args()
    data = load_rows(args.input)
    if not data:
        raise SystemExit(f"No usable data found in {args.input}")
    plot_data(data, args.output, args.title)
    print(f"Saved plot to {args.output}")


if __name__ == "__main__":
    main()
