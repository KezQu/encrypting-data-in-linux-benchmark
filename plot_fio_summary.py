#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

plt: Any = importlib.import_module("matplotlib.pyplot")
plt.switch_backend("Agg")

DEFAULT_INPUT = Path("fio_summary.csv")
DEFAULT_OUTPUT_DIR = Path("fio_summary_plots")

MECHANISM_ORDER = ["Raw I/O", "LUKS", "eCryptfs", "fscrypt"]
TEST_ORDER = [
    "seq_write_4k",
    "seq_read_4k",
    "seq_write_1M",
    "seq_read_1M",
    "rand_write_4k",
    "rand_read_4k",
    "rand_write_1M",
    "rand_read_1M",
    "mixed_70r_30w",
    "mixed_50r_50w",
]

MECHANISM_COLORS = {
    "Raw I/O": "blue",
    "LUKS": "orange",
    "eCryptfs": "green",
    "fscrypt": "red",
}

TEST_LABELS = {
    "seq_write_4k": "seq write\n4K",
    "seq_read_4k": "seq read\n4K",
    "seq_write_1M": "seq write\n1M",
    "seq_read_1M": "seq read\n1M",
    "rand_write_4k": "rand write\n4K",
    "rand_read_4k": "rand read\n4K",
    "rand_write_1M": "rand write\n1M",
    "rand_read_1M": "rand read\n1M",
    "mixed_70r_30w": "mixed 70/30\n1M",
    "mixed_50r_50w": "mixed 50/50\n1M",
}


@dataclass(frozen=True)
class SubplotSpec:
    label: str
    column: str
    ylabel: str
    kind: str
    log_scale: bool = False


@dataclass(frozen=True)
class PlotSpec:
    name: str
    title: str
    subplots: tuple[SubplotSpec, ...]


PLOT_SPECS = {
    "bandwidth": PlotSpec(
        name="bandwidth",
        title="Bandwidth comparison",
        subplots=(
            SubplotSpec("Read", "Read_BW_MBs", "Read BW [MB/s]", "read", True),
            SubplotSpec(
                "Write", "Write_BW_MBs", "Write BW [MB/s]", "write", True
            ),
        ),
    ),
    "iops": PlotSpec(
        name="iops",
        title="IOPS comparison",
        subplots=(
            SubplotSpec("Read", "Read_IOPS", "Read IOPS", "read", True),
            SubplotSpec("Write", "Write_IOPS", "Write IOPS", "write", True),
        ),
    ),
    "latency": PlotSpec(
        name="latency",
        title="Latency comparison",
        subplots=(
            SubplotSpec(
                "Read", "Read_Lat_us", "Read latency [us]", "read", True
            ),
            SubplotSpec(
                "Write", "Write_Lat_us", "Write latency [us]", "write", True
            ),
        ),
    ),
    "cpu": PlotSpec(
        name="cpu",
        title="CPU usage comparison",
        subplots=(
            SubplotSpec("User CPU", "CPU_usr_%", "User CPU [%]", "all", False),
            SubplotSpec(
                "System CPU", "CPU_sys_%", "System CPU [%]", "all", False
            ),
        ),
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate comparison charts from fio_summary.csv."
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
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for generated plots",
    )
    parser.add_argument(
        "--prefix",
        default="fio_summary",
        help="Filename prefix for generated charts",
    )
    parser.add_argument(
        "--metrics",
        nargs="*",
        choices=tuple(PLOT_SPECS.keys()),
        default=list(PLOT_SPECS.keys()),
        help="Subset of metrics to plot",
    )
    return parser.parse_args()


def mechanism_sort_key(name: str) -> tuple[int, str]:
    try:
        return (MECHANISM_ORDER.index(name), name)
    except ValueError:
        return (len(MECHANISM_ORDER), name)


def is_relevant_test(test_name: str, kind: str) -> bool:
    if kind == "all":
        return True
    if kind == "read":
        return ("read" in test_name) or ("mixed" in test_name)
    if kind == "write":
        return ("write" in test_name) or ("mixed" in test_name)
    return True


def load_rows(csv_path: Path) -> dict[str, dict[str, dict[str, str]]]:
    grouped: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            mechanism = row["Mechanism"].strip()
            test = row["Test"].strip()

            if test in grouped[mechanism]:
                raise ValueError(f"Duplicate row for {mechanism} / {test}")

            grouped[mechanism][test] = row

    return grouped


def collect_values(
    data: dict[str, dict[str, dict[str, str]]],
    column: str,
    kind: str,
) -> tuple[list[str], list[str], dict[str, list[float]]]:
    mechanisms = sorted(data.keys(), key=mechanism_sort_key)
    tests = [
        test
        for test in TEST_ORDER
        if any(test in mechanism_rows for mechanism_rows in data.values())
    ]
    tests = [test for test in tests if is_relevant_test(test, kind)]

    values: dict[str, list[float]] = {mechanism: [] for mechanism in mechanisms}
    for mechanism in mechanisms:
        mechanism_rows = data[mechanism]
        for test in tests:
            row = mechanism_rows.get(test)
            if row is None:
                values[mechanism].append(float("nan"))
                continue
            values[mechanism].append(float(row[column]))

    return mechanisms, tests, values


def format_axis(
    axis: Any,
    spec: SubplotSpec,
    tests: list[str],
    values: dict[str, list[float]],
) -> None:
    x_positions = list(range(len(tests)))
    mechanisms = list(values.keys())
    bar_width = 0.8 / max(len(mechanisms), 1)

    for index, mechanism in enumerate(mechanisms):
        offsets = [
            x + (index - (len(mechanisms) - 1) / 2) * bar_width
            for x in x_positions
        ]
        axis.bar(
            offsets,
            values[mechanism],
            width=bar_width,
            label=mechanism,
            color=MECHANISM_COLORS.get(mechanism),
            edgecolor="black",
            linewidth=0.4,
        )

    axis.set_title(spec.label)
    axis.set_ylabel(spec.ylabel)
    axis.set_xticks(x_positions)
    axis.set_xticklabels(
        [TEST_LABELS.get(test, test) for test in tests], rotation=0
    )
    axis.grid(True, axis="y", linestyle="--", linewidth=0.6, alpha=0.5)
    axis.margins(x=0.02)

    if spec.log_scale:
        axis.set_yscale("log")


def draw_plot(
    data: dict[str, dict[str, dict[str, str]]],
    spec: PlotSpec,
    output_path: Path,
) -> None:
    cols = len(spec.subplots)
    fig, axes = plt.subplots(
        1,
        cols,
        figsize=(8.5 * cols, 5.2),
        constrained_layout=True,
    )

    axes_list = [axes] if cols == 1 else list(axes.flat)
    legend_handles = None
    legend_labels = None

    for axis, subplot in zip(axes_list, spec.subplots, strict=True):
        _, tests, values = collect_values(data, subplot.column, subplot.kind)
        format_axis(axis, subplot, tests, values)
        axis.set_xlabel("Test")

        if legend_handles is None:
            legend_handles, legend_labels = axis.get_legend_handles_labels()

    if legend_handles and legend_labels:
        fig.legend(
            legend_handles,
            legend_labels,
            loc="upper center",
            ncol=min(len(legend_labels), 4),
            frameon=False,
            bbox_to_anchor=(0.5, 1.08),
        )

    fig.suptitle(spec.title, fontsize=16, fontweight="bold")
    fig.savefig(str(output_path), dpi=220, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    args = parse_args()

    if not args.input.exists():
        raise SystemExit(f"Input file not found: {args.input}")

    data = load_rows(args.input)
    if not data:
        raise SystemExit(f"No usable data found in {args.input}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    for metric_name in args.metrics:
        spec = PLOT_SPECS[metric_name]
        output_path = args.output_dir / f"{args.prefix}_{spec.name}.png"
        draw_plot(data, spec, output_path)
        print(f"Saved plot to {output_path}")


if __name__ == "__main__":
    main()
