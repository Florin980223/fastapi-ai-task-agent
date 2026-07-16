"""The evaluation report: reproducibility metadata + scores, as both a
JSON-serializable structure and a terminal summary.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from evals.scoring import AggregateScores, MetricScore


@dataclass
class Report:
    dataset_version: str
    dataset_path: str
    mode: str
    mode_description: str
    measures_model_quality: bool
    model_name: str | None
    git_commit: str | None
    thresholds: dict[str, float]
    total_cases: int
    passed_cases: int
    overall_case_accuracy: float
    metrics: dict[str, MetricScore]
    categories: dict[str, dict]
    failed_cases: list[dict] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "dataset_version": self.dataset_version,
            "dataset_path": self.dataset_path,
            "mode": self.mode,
            "mode_description": self.mode_description,
            "measures_model_quality": self.measures_model_quality,
            "model_name": self.model_name,
            "generated_at": self.generated_at,
            "git_commit": self.git_commit,
            "thresholds": self.thresholds,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "overall_case_accuracy": self.overall_case_accuracy,
            "metrics": {
                name: {"score": metric.score, "passed": metric.passed, "applicable": metric.applicable}
                for name, metric in self.metrics.items()
            },
            "categories": self.categories,
            "failed_cases": self.failed_cases,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def build_report(
    *,
    scores: AggregateScores,
    dataset_version: str,
    dataset_path: str,
    mode: str,
    mode_description: str,
    measures_model_quality: bool,
    model_name: str | None,
    git_commit: str | None,
    thresholds: dict[str, float],
) -> Report:
    return Report(
        dataset_version=dataset_version,
        dataset_path=dataset_path,
        mode=mode,
        mode_description=mode_description,
        measures_model_quality=measures_model_quality,
        model_name=model_name,
        git_commit=git_commit,
        thresholds=thresholds,
        total_cases=scores.total_cases,
        passed_cases=scores.passed_cases,
        overall_case_accuracy=scores.overall_case_accuracy,
        metrics=scores.metrics,
        categories=scores.categories,
        failed_cases=scores.failed_cases,
    )


def print_summary(report: Report) -> None:
    print(f"Evaluation mode: {report.mode}")
    print(f"  {report.mode_description}")
    print(f"  Measures real model quality: {report.measures_model_quality}")
    if report.model_name:
        print(f"  Model: {report.model_name}")
    print(f"Dataset: {report.dataset_path} (version {report.dataset_version})")
    if report.git_commit:
        print(f"Git commit: {report.git_commit}")
    print(f"Generated at: {report.generated_at}")
    print(f"Thresholds: {report.thresholds}")
    print()
    print(f"Overall case accuracy: {report.overall_case_accuracy:.1%} ({report.passed_cases}/{report.total_cases})")
    print()
    print("Metrics:")
    for name, metric in report.metrics.items():
        if metric.score is None:
            print(f"  {name}: [no applicable cases]")
        else:
            print(f"  {name}: {metric.score:.1%} ({metric.passed}/{metric.applicable})")
    print()
    print("By category:")
    for category, stats in sorted(report.categories.items()):
        accuracy = stats.get("accuracy")
        accuracy_str = f"{accuracy:.1%}" if accuracy is not None else "n/a"
        print(f"  {category}: {accuracy_str} ({stats['passed']}/{stats['total']})")

    if report.failed_cases:
        print()
        print(f"Failed cases ({len(report.failed_cases)}):")
        for failed in report.failed_cases:
            print(f"  [{failed['id']}] ({failed['category']}, {failed['language']}) {failed['message']!r}")
            for mismatch in failed["mismatches"]:
                print(f"      - {mismatch}")
