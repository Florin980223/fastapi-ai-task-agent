"""Shared argument schemas and validation for LLM-based decision providers.

Both the Anthropic and Ollama providers need the same information for
each executable tool: a JSON Schema describing its arguments (for the
provider's native tool-calling request) and the same rules for
validating whatever the model calls back with. Keeping both here means
the two providers can't silently drift apart on what's "required" or
"the right type" for a given tool.
"""

import re
from datetime import date

_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# JSON Schema arguments for each executable tool. This is provider-neutral:
# Anthropic wraps it as a tool's "input_schema", Ollama/OpenAI-style wraps
# it as a function's "parameters" - the schema itself is identical either way.
# task_title lets a caller identify a task by name instead of by numeric
# id, for the three tools in TASK_ID_OR_TITLE_TOOLS below. Exactly one of
# task_id/task_title is needed - never both required - which is why
# neither is listed in these three schemas' "required" array; instead
# each carries an "anyOf" constraint spelling out the OR-relationship, so
# a schema-aware model is never told task_id is unconditionally mandatory
# when a title reference is just as valid. app.services.task_resolution
# resolves task_title into a task_id before anything else (missing-
# argument checks, validation, execution) ever runs - REQUIRED_ARGUMENTS
# below is unaffected by this and still says task_id is what execution
# ultimately needs, since it's only consulted after that resolution step
# has already had its chance to run.
_TASK_ID_OR_TITLE_ANY_OF = [{"required": ["task_id"]}, {"required": ["task_title"]}]
_TASK_TITLE_PROPERTY = {
    "type": "string",
    "description": "The task's title, or a short phrase referring to it, if its numeric id is not known.",
}

# Canonical priority values - single source of truth for the JSON schema
# enum below and validate_tool_call's runtime check.
PRIORITY_VALUES: frozenset[str] = frozenset({"low", "medium", "high"})
_PRIORITY_PROPERTY = {
    "type": "string",
    "enum": sorted(PRIORITY_VALUES),
    "description": "Task priority - must be exactly one of low/medium/high.",
}
_DUE_DATE_PROPERTY = {
    "type": ["string", "null"],
    "description": (
        "Due date as an ISO calendar date, YYYY-MM-DD (e.g. 2026-08-15) - no other format, "
        "and never a relative phrase like 'Friday' or 'next week'. Pass null to clear an "
        "existing due date. Omit this field entirely to leave the due date unchanged."
    ),
}
# update_task must have exactly one task selector (task_id or task_title,
# unchanged from before) AND at least one requested mutation (a new
# title, priority, or due_date) - two independent OR-constraints, so
# top-level "required" can no longer name "title" unconditionally (a
# priority-only or due-date-only update has no new title at all). Both
# constraints are still expressed for the LLM providers' benefit only -
# neither is enforced by validate_tool_call, exactly like
# _TASK_ID_OR_TITLE_ANY_OF already wasn't; the real enforcement is
# app.services.clarification.missing_arguments.
_TASK_MUTATION_ANY_OF = [{"required": ["title"]}, {"required": ["priority"]}, {"required": ["due_date"]}]

TOOL_ARGUMENT_SCHEMAS: dict[str, dict] = {
    "create_task": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The task title."},
            "priority": _PRIORITY_PROPERTY,
            "due_date": _DUE_DATE_PROPERTY,
        },
        "required": ["title"],
    },
    "list_tasks": {
        "type": "object",
        "properties": {
            "done": {
                "type": ["boolean", "null"],
                "description": "Filter by completion status. Omit or null for all tasks.",
            }
        },
    },
    "get_weather": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "The city to get the weather for."}},
        "required": ["city"],
    },
    "mark_task_done": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "The id of the task to mark done."},
            "task_title": _TASK_TITLE_PROPERTY,
        },
        "anyOf": _TASK_ID_OR_TITLE_ANY_OF,
    },
    "update_task": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "The id of the task to update."},
            "task_title": _TASK_TITLE_PROPERTY,
            "title": {"type": "string", "description": "The new title for the task."},
            "priority": _PRIORITY_PROPERTY,
            "due_date": _DUE_DATE_PROPERTY,
        },
        "allOf": [{"anyOf": _TASK_ID_OR_TITLE_ANY_OF}, {"anyOf": _TASK_MUTATION_ANY_OF}],
    },
    "delete_task": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "The id of the task to delete."},
            "task_title": _TASK_TITLE_PROPERTY,
        },
        "anyOf": _TASK_ID_OR_TITLE_ANY_OF,
    },
}

# Required arguments and their expected Python types, used to validate a
# model's tool call before trusting it.
REQUIRED_ARGUMENTS: dict[str, dict[str, type]] = {
    "create_task": {"title": str},
    "list_tasks": {},
    "get_weather": {"city": str},
    "mark_task_done": {"task_id": int},
    "update_task": {"task_id": int, "title": str},
    "delete_task": {"task_id": int},
}

# The one tool whose execution is irreversible enough to require the user to
# explicitly say yes before it runs. Single source of truth - clarification.py
# and agent_planner.py both derive from this instead of each keeping their
# own copy.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({"delete_task"})

# Name of the alternative-to-task_id argument, above. Single source of
# truth so callers never hardcode the string.
TASK_TITLE_ARGUMENT = "task_title"

# Tools that can identify their target task either by task_id or by
# task_title. Single source of truth - app.services.task_resolution and
# app.routes.agent both derive from this instead of each keeping their own
# copy, the same pattern as DESTRUCTIVE_TOOLS above.
TASK_ID_OR_TITLE_TOOLS: frozenset[str] = frozenset({"mark_task_done", "update_task", "delete_task"})


class ToolCallValidationError(Exception):
    """Raised when a model's tool call fails validation.

    Each provider catches this and re-raises it as its own
    provider-specific error, which is what agent_decision.decide_tool
    actually catches to trigger a fallback.
    """


def validate_tool_call(tool_name: str | None, arguments: object) -> None:
    """Validate a model-selected tool name and its arguments' shape.

    Checks: the tool name is one of the executable tools, arguments is a
    dict, every key in arguments is one this tool actually accepts, and
    any argument that IS present (and not None) has a reasonable type.
    Raises ToolCallValidationError on any of those problems - these
    represent the model malfunctioning/hallucinating, which should
    trigger a fallback to another decision provider (subject to
    agent_decision's fallback-safety gate, not unconditionally).

    Deliberately does NOT check that required arguments are present: a
    model that correctly identifies a tool but omits (or explicitly
    sends None for) a required argument returns a valid, if incomplete,
    decision - that's not a provider failure. See
    app/services/clarification.py, which uses REQUIRED_ARGUMENTS to
    detect that case and ask the user instead of executing.

    This function is called both inside each LLM provider (right after
    parsing the model's response) and again, independently, at the
    execution layer (app/routes/agent.py, app/services/agent_planner.py)
    - so this one change protects both without needing separate logic in
    either place.
    """
    if tool_name not in REQUIRED_ARGUMENTS:
        raise ToolCallValidationError(f"Model selected an unknown tool: {tool_name!r}.")

    if not isinstance(arguments, dict):
        raise ToolCallValidationError(f"Model returned non-dict arguments for '{tool_name}'.")

    allowed_arg_names = TOOL_ARGUMENT_SCHEMAS[tool_name]["properties"].keys()
    for arg_name in arguments:
        if arg_name not in allowed_arg_names:
            raise ToolCallValidationError(f"Model's call to '{tool_name}' has an unsupported argument: {arg_name!r}.")

    for arg_name, expected_type in REQUIRED_ARGUMENTS[tool_name].items():
        value = arguments.get(arg_name)
        if value is not None and not isinstance(value, expected_type):
            raise ToolCallValidationError(f"Model's call to '{tool_name}' has the wrong type for '{arg_name}'.")

    # priority/due_date are optional (never in REQUIRED_ARGUMENTS above),
    # but their *value* - whenever the model does supply one - must still
    # be constrained: an optional argument is not the same as an
    # unconstrained one. Checked here, independently of required-ness, so
    # a model hallucinating priority="urgent" or due_date="next Friday"
    # is caught the same way a wrong-type required argument already is -
    # never silently coerced or passed through to execution. Deliberately
    # phrased to include "wrong type" so
    # decision_validation.classify_validation_failure categorizes this as
    # "wrong_type", which is exactly the category agent_decision's
    # _safe_to_fall_back already refuses to silently re-derive via
    # rule_based - the right behavior for a bad planning-field value too.
    if "priority" in arguments:
        priority_value = arguments["priority"]
        if priority_value is not None and priority_value not in PRIORITY_VALUES:
            raise ToolCallValidationError(
                f"Model's call to '{tool_name}' has the wrong type for 'priority': "
                f"must be one of {sorted(PRIORITY_VALUES)}, got {priority_value!r}."
            )

    if "due_date" in arguments:
        due_date_value = arguments["due_date"]
        if due_date_value is not None and not is_valid_iso_date(due_date_value):
            raise ToolCallValidationError(
                f"Model's call to '{tool_name}' has the wrong type for 'due_date': "
                f"must be an ISO YYYY-MM-DD date string or null, got {due_date_value!r}."
            )


def is_valid_iso_date(value: object) -> bool:
    """Whether `value` is a string of the exact form YYYY-MM-DD naming a
    real calendar date. Public (not module-private) and deliberately
    dependency-free of agent_decision.py/clarification.py - both of those
    import this as the one shared definition of "is this an explicit,
    unambiguous calendar date" (agent_decision for its own rule-based
    extraction, clarification for parsing a reply to a due-date
    clarification question) - importing either of them from here, or each
    other, would be circular.
    """
    if not isinstance(value, str) or not _ISO_DATE_PATTERN.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True
