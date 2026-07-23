"""Tests for the task_id / task_title OR-relationship in tool_schemas.py.

Two independent dicts answer two independent questions, and this file
exists specifically to pin that they never contradict each other:

- TOOL_ARGUMENT_SCHEMAS[tool]["required"]/["anyOf"] describes what a raw
  model tool-call must contain (an "anyOf" saying "at least one of
  task_id/task_title", never a hard requirement on task_id alone, for
  the three tools that support title resolution).
- REQUIRED_ARGUMENTS describes what execution ultimately needs (still
  task_id) - consulted only by clarification.missing_arguments/
  validate_tool_call's type-check loop, both of which run *after*
  app.services.task_resolution has already had a chance to turn a
  task_title into a task_id (see tests/test_agent_execute.py for the
  end-to-end proof of that ordering).
"""

import pytest

from app.services import tool_schemas
from app.services.tool_schemas import ToolCallValidationError, validate_tool_call

TASK_ID_OR_TITLE_TOOLS = ("mark_task_done", "update_task", "delete_task")


@pytest.mark.parametrize("tool_name", TASK_ID_OR_TITLE_TOOLS)
def test_task_id_alone_is_valid(tool_name):
    arguments = {"task_id": 1}
    if tool_name == "update_task":
        arguments["title"] = "New title"
    validate_tool_call(tool_name, arguments)  # must not raise


@pytest.mark.parametrize("tool_name", TASK_ID_OR_TITLE_TOOLS)
def test_task_title_alone_is_valid(tool_name):
    arguments = {"task_title": "the portfolio task"}
    if tool_name == "update_task":
        arguments["title"] = "New title"
    validate_tool_call(tool_name, arguments)  # must not raise


@pytest.mark.parametrize("tool_name", TASK_ID_OR_TITLE_TOOLS)
def test_neither_task_id_nor_task_title_does_not_raise_in_validate_tool_call(tool_name):
    # validate_tool_call deliberately never checks presence of required
    # arguments (see its docstring) - "neither present" is a valid, if
    # incomplete, decision here. It's clarification.missing_arguments'
    # job (tested in test_clarification.py / test_agent_execute.py) to
    # turn this into a controlled clarification instead of executing.
    arguments = {"title": "New title"} if tool_name == "update_task" else {}
    validate_tool_call(tool_name, arguments)  # must not raise


@pytest.mark.parametrize("tool_name", TASK_ID_OR_TITLE_TOOLS)
def test_both_task_id_and_task_title_is_valid_at_the_schema_level(tool_name):
    # validate_tool_call has no opinion on precedence between the two -
    # that's app.services.task_resolution's job (task_id always wins,
    # see test_task_resolution.py's noop-when-task_id-present tests).
    arguments = {"task_id": 1, "task_title": "the portfolio task"}
    if tool_name == "update_task":
        arguments["title"] = "New title"
    validate_tool_call(tool_name, arguments)  # must not raise


@pytest.mark.parametrize("tool_name", TASK_ID_OR_TITLE_TOOLS)
def test_required_arguments_still_requires_task_id_post_resolution(tool_name):
    # Pins the "no OR-type" design: REQUIRED_ARGUMENTS is untouched by
    # the title-resolution feature - it still says task_id is what
    # execution ultimately needs, since it's only consulted after the
    # resolution seam in routes/agent.py has already run.
    assert tool_schemas.REQUIRED_ARGUMENTS[tool_name]["task_id"] is int


@pytest.mark.parametrize("tool_name", TASK_ID_OR_TITLE_TOOLS)
def test_task_title_is_not_in_required_arguments(tool_name):
    # task_title is an alternative, not an addition - it must never be
    # independently "required" in the internal presence-check map,
    # otherwise a task_id-only call would wrongly be reported incomplete.
    assert "task_title" not in tool_schemas.REQUIRED_ARGUMENTS[tool_name]


@pytest.mark.parametrize("tool_name", TASK_ID_OR_TITLE_TOOLS)
def test_schema_anyof_expresses_task_id_or_task_title(tool_name):
    schema = tool_schemas.TOOL_ARGUMENT_SCHEMAS[tool_name]
    # update_task's selector anyOf now lives nested inside "allOf"
    # alongside a second anyOf for "at least one mutation" (see below) -
    # mark_task_done/delete_task are unaffected and keep the plain
    # top-level "anyOf".
    if tool_name == "update_task":
        assert schema["allOf"][0]["anyOf"] == [{"required": ["task_id"]}, {"required": ["task_title"]}]
    else:
        assert schema["anyOf"] == [{"required": ["task_id"]}, {"required": ["task_title"]}]
    assert "task_id" not in schema.get("required", [])


def test_update_task_title_is_no_longer_unconditionally_required():
    # A priority-only or due-date-only update (no new title at all) is a
    # valid request - title no longer has a top-level "required" entry;
    # it's one of three alternatives (title/priority/due_date), at least
    # one of which must be present, expressed as a second "anyOf" nested
    # in "allOf" alongside the existing task_id/task_title selector anyOf.
    schema = tool_schemas.TOOL_ARGUMENT_SCHEMAS["update_task"]
    assert "required" not in schema
    assert schema["allOf"][1]["anyOf"] == [
        {"required": ["title"]},
        {"required": ["priority"]},
        {"required": ["due_date"]},
    ]
    # Still type-checked (via REQUIRED_ARGUMENTS) whenever it IS given -
    # only its unconditional presence requirement was relaxed.
    assert tool_schemas.REQUIRED_ARGUMENTS["update_task"]["title"] is str


@pytest.mark.parametrize("tool_name", ("create_task", "list_tasks", "get_weather"))
def test_tools_outside_scope_have_no_task_title_property(tool_name):
    schema = tool_schemas.TOOL_ARGUMENT_SCHEMAS[tool_name]
    assert "task_title" not in schema["properties"]
    assert "anyOf" not in schema


def test_task_id_or_title_tools_constant_matches_expected_set():
    assert tool_schemas.TASK_ID_OR_TITLE_TOOLS == frozenset(TASK_ID_OR_TITLE_TOOLS)


def test_validate_tool_call_still_rejects_unknown_argument():
    with pytest.raises(ToolCallValidationError):
        validate_tool_call("mark_task_done", {"task_id": 1, "priority": "high"})
