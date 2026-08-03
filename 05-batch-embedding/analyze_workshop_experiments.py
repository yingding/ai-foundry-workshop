import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt


@dataclass(frozen=True)
class Experiment:
    label: str
    report: Path


def load_metrics(experiment: Experiment) -> dict[str, float]:
    payload = json.loads(experiment.report.read_text(encoding="utf-8"))
    prefix = payload["metric_prefix"] + "."
    return {
        name.removeprefix(prefix): float(value)
        for name, value in payload["metrics"].items()
        if name.startswith(prefix)
    }


def label_bars(axis, bars, *, suffix: str = "", decimals: int = 0) -> None:
    for bar in bars:
        value = bar.get_height()
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            value,
            f"{value:,.{decimals}f}{suffix}",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
        )


def build_chart(input_dir: Path, output_path: Path) -> None:
    reports = {
        "rpm_none": Experiment("One input/request", input_dir / "rpm-none-metrics.json"),
        "rpm_packed": Experiment(
            "Packed array", input_dir / "rpm-packed-v2-metrics.json"
        ),
        "direct_overload": Experiment(
            "Direct overload", input_dir / "tpm-direct-overload-metrics.json"
        ),
        "direct": Experiment(
            "Direct 60% plan", input_dir / "tpm-direct-metrics.json"
        ),
        "pool": Experiment("APIM pool 60% plan", input_dir / "tpm-pool-metrics.json"),
    }
    metrics = {name: load_metrics(report) for name, report in reports.items()}

    blue = "#1565C0"
    cyan = "#00838F"
    amber = "#F9A825"
    red = "#C62828"
    gray = "#546E7A"

    figure, axes = plt.subplots(1, 3, figsize=(16, 5.4))
    figure.patch.set_facecolor("white")
    figure.suptitle(
        "Batch Embedding Workshop: Experiment Evidence",
        fontsize=17,
        fontweight="bold",
    )

    rpm_values = [
        metrics["rpm_none"]["attempted_requests"],
        metrics["rpm_packed"]["attempted_requests"],
    ]
    bars = axes[0].bar(
        [reports["rpm_none"].label, reports["rpm_packed"].label],
        rpm_values,
        color=[gray, blue],
    )
    axes[0].set_title("RPM experiment\nSame 100 logical inputs")
    axes[0].set_ylabel("Client HTTP requests")
    axes[0].set_ylim(0, max(rpm_values) * 1.18)
    label_bars(axes[0], bars)
    reduction = 1 - rpm_values[1] / rpm_values[0]
    axes[0].text(
        0.5,
        0.88,
        f"{reduction:.0%} request reduction",
        transform=axes[0].transAxes,
        ha="center",
        color=blue,
        fontweight="bold",
    )

    tpm_values = [
        metrics["direct"]["accepted_tpm"],
        metrics["pool"]["accepted_tpm"],
    ]
    bars = axes[1].bar(
        [reports["direct"].label, reports["pool"].label],
        tpm_values,
        color=[cyan, blue],
    )
    axes[1].set_title("TPM experiment\nSame 400 inputs and 17,680 tokens")
    axes[1].set_ylabel("Accepted tokens/minute")
    axes[1].set_ylim(0, max(tpm_values) * 1.18)
    label_bars(axes[1], bars)
    improvement = tpm_values[1] / tpm_values[0] - 1
    axes[1].text(
        0.5,
        0.88,
        f"{improvement:.1%} pooled increase",
        transform=axes[1].transAxes,
        ha="center",
        color=blue,
        fontweight="bold",
    )

    throttle_values = [
        metrics["direct_overload"]["throttle_rate"] * 100,
        metrics["direct"]["throttle_rate"] * 100,
        metrics["pool"]["throttle_rate"] * 100,
    ]
    bars = axes[2].bar(
        [
            reports["direct_overload"].label,
            reports["direct"].label,
            reports["pool"].label,
        ],
        throttle_values,
        color=[red, cyan, blue],
    )
    axes[2].set_title("Admission behavior\nExplicit call-rate feedback")
    axes[2].set_ylabel("HTTP 429 throttle rate (%)")
    axes[2].set_ylim(0, max(throttle_values) * 1.22)
    label_bars(axes[2], bars, suffix="%", decimals=1)

    for axis in axes:
        axis.grid(axis="y", alpha=0.22)
        axis.spines[["top", "right"]].set_visible(False)
        axis.tick_params(axis="x", rotation=12)

    figure.text(
        0.5,
        0.01,
        "Separate axes show separate claims: request efficiency, sustained token throughput, and throttling.",
        ha="center",
        fontsize=10,
    )
    figure.tight_layout(rect=(0, 0.05, 1, 0.93))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)
    print(f"Wrote experiment chart to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the workshop experiment analysis chart from AML metric exports."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/experiment-metrics"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/imgs/workshop-experiment-analysis.png"),
    )
    args = parser.parse_args()
    build_chart(args.input_dir, args.output)


if __name__ == "__main__":
    main()
