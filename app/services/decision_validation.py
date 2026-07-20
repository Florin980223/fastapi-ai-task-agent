"""Shared control-flow helpers for the LLM-backed decision providers
(Anthropic, Ollama single-step decision, Ollama planner) - never used by,
or applied to, the deterministic rule_based provider. rule_based's own
decisions, tests, and eval baseline are untouched by anything here.

Provides:
- DecisionProviderError: the common exception base every LLM-provider
  error subclasses, so the dispatcher (agent_decision.decide_tool) can
  catch one exception type instead of duplicating a try/except block per
  provider.
- attempt_with_repair: exactly one constrained repair retry on a
  parse/validation failure - never on a network/timeout failure, and
  never more than once, no matter how the repair attempt itself turns
  out.
- classify_validation_failure: a best-effort, fixed-vocabulary category
  string for structured logging only - never raises, never used for
  control flow, and never includes any request/response content, only a
  category name.
"""

import json
from typing import Callable, TypeVar

from pydantic import ValidationError

from app.services.tool_schemas import ToolCallValidationError

T = TypeVar("T")

# The only failure shapes a repair attempt can plausibly fix: the model's
# JSON didn't parse, its tool call didn't validate (tool_schemas), or (for
# the planner) the JSON parsed but didn't match AgentPlan's shape. A
# network/timeout exception is deliberately NOT in this tuple - see
# attempt_with_repair's docstring.
_REPAIRABLE_FAILURES = (ToolCallValidationError, json.JSONDecodeError, ValidationError)


class DecisionProviderError(Exception):
    """Common base for every LLM-backed decision/planning provider error.

    Exists so agent_decision.decide_tool can catch one exception type
    instead of one per provider. Each provider still defines its own
    subclass (AnthropicDecisionError, OllamaDecisionError,
    OllamaPlanningError) for anything that wants to distinguish them.

    Carries an explicit `category` (one of classify_validation_failure's
    fixed vocabulary, or None) set by the provider at the point it raises -
    this is how agent_decision's fallback-safety gate learns *why* a
    provider failed without needing to fragilely re-derive it from
    `__cause__` (which is None for a provider's own direct raises, e.g.
    "no tool was called", that have no underlying exception to chain).
    """

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category


def attempt_with_repair(primary: Callable[[], T], repair: Callable[[Exception], T]) -> T:
    """Run `primary()`; on a parse/validation-shaped failure, run
    `repair(exc)` exactly once and return its result (or let it raise).

    Deliberately narrow: only catches the failure shapes listed in
    _REPAIRABLE_FAILURES. A network/timeout exception (e.g.
    httpx.ConnectError, httpx.TimeoutException, the Anthropic SDK's own
    connection errors) is never caught here, so a dead server never gets
    a second, equally-doomed attempt under the same timeout - it
    propagates straight to the caller's own broad except block exactly as
    it did before this helper existed. Timeouts and connection failures
    must reach the caller's fallback-safety gate exactly the same way a
    malformed-output failure does - they are never given an easier path.

    Never loops, never retries the repair attempt itself: there is no
    parameter to increase the retry count, and no code path here calls
    `repair` more than once. If `repair` itself raises, that exception
    propagates normally to the caller - this is the entire mechanism
    behind "exactly one repair attempt, never a retry loop."

    Latency budget (documented here, not separately enforced by a new
    timeout): in the worst case - primary fails with a repairable error,
    then repair also fails - total wall-clock time for one decision is
    bounded by the sum of the two calls' own configured timeouts (each
    independently already bounded by OLLAMA_TIMEOUT_SECONDS or
    ANTHROPIC_TIMEOUT_SECONDS). No additional wrapping timeout is added;
    capping this helper at exactly one repair call is what keeps the
    total bounded.
    """
    try:
        return primary()
    except _REPAIRABLE_FAILURES as exc:
        return repair(exc)


def classify_validation_failure(exc: Exception) -> str:
    """Best-effort, fixed-vocabulary category for a provider failure -
    used only for structured logging (see each provider's log line).

    Never raises. Never used for control flow. Critically: never includes
    any part of `exc`'s own message in its return value (that message
    could echo back a fragment of the model's raw output) - only ever one
    of a small, fixed set of category strings.
    """
    if isinstance(exc, json.JSONDecodeError):
        return "malformed_json"

    if isinstance(exc, ValidationError):
        return "invalid_plan_shape"

    if isinstance(exc, ToolCallValidationError):
        message = str(exc)
        if "unknown tool" in message:
            return "unknown_tool"
        if "unsupported argument" in message:
            return "unknown_argument"
        if "wrong type" in message:
            return "wrong_type"
        return "malformed_json"

    # Anything else reaching here is a network/timeout-shaped failure
    # (httpx.ConnectError, httpx.TimeoutException, an Anthropic SDK
    # connection error, etc.) - distinguished only by exception class
    # name, never by message content, to avoid ever inspecting/echoing
    # provider-specific error text that might embed request details.
    exception_type_name = type(exc).__name__.lower()
    if "timeout" in exception_type_name:
        return "timeout"
    return "network_error"
