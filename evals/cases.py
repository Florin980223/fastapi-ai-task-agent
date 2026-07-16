"""The evaluation dataset schema and loader.

Cases live in evals/data/cases_v1.jsonl - one JSON object per line,
version-named by filename. DATASET_VERSION here must be bumped by hand
whenever the dataset's meaning changes (new fields, renamed categories,
etc.), independent of ordinary case additions/tweaks.
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

DATASET_VERSION = "v1"
DEFAULT_DATASET_PATH = Path(__file__).parent / "data" / "cases_v1.jsonl"

# The 7 evaluation categories from the eval plan. Kept as a plain set
# (not a Literal type) so a new category can be added to the dataset
# without a code change here - categories are free-form strings, this is
# just the documented, expected vocabulary.
CATEGORIES = {
    "single_step_tool_selection",
    "argument_extraction",
    "clarification_behavior",
    "destructive_confirmation",
    "no_tool_messages",
    "safe_multi_step_planning",
    "destructive_multi_step_rejection",
}


class SetupTask(BaseModel):
    """A task created (via the real POST /tasks endpoint) before a
    case's message(s), so a case like "Delete task 3" can rely on task
    id 3 deterministically existing.
    """

    title: str
    description: str | None = None
    done: bool = False


class PlannedStep(BaseModel):
    """One expected step of a multi-step plan - tool name and the
    arguments it should actually run with.
    """

    tool: str
    arguments: dict = Field(default_factory=dict)


class ExpectedOutcome(BaseModel):
    """What a correct agent response should look like for this case."""

    selected_tool: str | None = None
    arguments: dict | None = None
    needs_clarification: bool = False
    needs_confirmation: bool = False
    is_multi_step: bool = False
    step_tools: list[PlannedStep] | None = None


class ExpectedSideEffects(BaseModel):
    """The real, final database state after a case's turn(s) complete,
    fetched via GET /tasks. Every field is independently optional - only
    the fields actually present on a case are checked (see
    evals/scoring.py); omitting this whole block entirely means side
    effects are not checked for that case at all.
    """

    task_count: int | None = None
    task_ids_present: list[int] | None = None
    task_ids_absent: list[int] | None = None
    tasks_done: list[int] | None = None


class EvalCase(BaseModel):
    id: str
    category: str
    language: str
    setup_tasks: list[SetupTask] = Field(default_factory=list)
    message: str | None = None
    turns: list[str] | None = None
    expected: ExpectedOutcome
    expected_side_effects: ExpectedSideEffects | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _exactly_one_message_source(self) -> "EvalCase":
        if (self.message is None) == (self.turns is None):
            raise ValueError(f"case {self.id!r}: exactly one of 'message' or 'turns' must be set")
        if self.turns is not None and len(self.turns) == 0:
            raise ValueError(f"case {self.id!r}: 'turns' must not be empty")
        return self

    @property
    def effective_turns(self) -> list[str]:
        """The ordered list of messages to send, regardless of whether
        the case used 'message' or 'turns'.
        """
        return self.turns if self.turns is not None else [self.message]


def load_cases(path: Path | str = DEFAULT_DATASET_PATH) -> list[EvalCase]:
    """Parse a JSONL dataset file into typed, validated EvalCase objects.

    Raises ValueError naming the file and line number on a malformed
    entry, rather than letting a raw JSONDecodeError/ValidationError
    surface without context.
    """
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Evaluation dataset not found: {path}")

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            try:
                case = EvalCase.model_validate(raw)
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: invalid case: {exc}") from exc
            if case.id in seen_ids:
                raise ValueError(f"{path}:{line_number}: duplicate case id {case.id!r}")
            seen_ids.add(case.id)
            cases.append(case)

    return cases
