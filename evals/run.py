"""CLI entry point for the evaluation suite.

Usage:
    python -m evals.run
    python -m evals.run --mode mocked-ollama
    python -m evals.run --mode live-ollama --allow-live-ollama

Exit codes: 0 thresholds met, 1 thresholds not met, 2 usage/setup error.
"""

import argparse
import sys
from pathlib import Path

from evals.cases import DEFAULT_DATASET_PATH
from evals.report import print_summary
from evals.runner import run_evaluation

# The dataset includes Romanian text (diacritics like ș/ă/î), and some
# terminals (notably Windows' legacy cp1252 console) can't encode those
# characters at all - printing one would crash the whole run with an
# uncaught UnicodeEncodeError. Reconfiguring stdout/stderr to UTF-8 makes
# the CLI's terminal output robust regardless of the host console's
# default encoding.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the agent's offline evaluation suite (separate from pytest).")
    parser.add_argument(
        "--mode",
        choices=["rule_based", "mocked-ollama", "live-ollama"],
        default="rule_based",
        help="rule_based (default): real rule-based logic, fully offline. "
        "mocked-ollama: validates the pipeline/provider contract with a scripted stub, fully offline. "
        "live-ollama: evaluates a real local Ollama model - requires --allow-live-ollama and a running server.",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH, help="Path to a cases JSONL file.")
    parser.add_argument("--report-path", type=Path, default=None, help="Where to write the JSON report (default: evals/reports/<mode>-<timestamp>.json).")
    parser.add_argument("--min-overall-accuracy", type=float, default=None, help="Override this mode's default overall_case_accuracy threshold.")
    parser.add_argument("--min-safety-accuracy", type=float, default=None, help="Override this mode's default safety_accuracy threshold.")
    parser.add_argument(
        "--allow-live-ollama",
        action="store_true",
        help="Required in addition to --mode live-ollama - that mode makes real requests to a local Ollama server.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.mode == "live-ollama" and not args.allow_live_ollama:
        print(
            "Error: --mode live-ollama also requires --allow-live-ollama (it makes real requests "
            "to a local Ollama server). No requests were made.",
            file=sys.stderr,
        )
        return 2

    try:
        report, exit_code, report_path = run_evaluation(
            mode=args.mode,
            dataset_path=args.dataset,
            min_overall_accuracy=args.min_overall_accuracy,
            min_safety_accuracy=args.min_safety_accuracy,
            report_path=args.report_path,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print_summary(report)
    print()
    print(f"Report written to: {report_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
