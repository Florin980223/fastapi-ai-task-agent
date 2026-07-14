"""Very simple rule-based "tool selection" and "tool execution" for
user messages.

This does NOT call any AI/LLM. Tool selection just checks the message
text for a few keywords, in order, and picks the first tool whose
keywords match. Tool execution then runs a small, hardcoded set of
tools using the existing task/weather services. Both are stand-ins for
a future version where an actual AI model makes these decisions.
"""

import re

from app.schemas import TaskResponse
from app.services import task_service, weather_service

# Each rule is (tool_name, keywords, reason). Rules are checked in
# order, and the first one whose keyword appears in the message wins.
RULES: list[tuple[str, list[str], str]] = [
    (
        "get_weather",
        ["weather", "temperature", "forecast", "city weather"],
        "The user is asking about weather.",
    ),
    (
        "create_task",
        ["create", "add", "new task", "todo"],
        "The user wants to create a new task.",
    ),
    (
        "list_tasks",
        ["list", "show tasks", "all tasks", "completed tasks", "unfinished tasks"],
        "The user wants to see a list of tasks.",
    ),
    (
        "update_task",
        ["update", "edit", "change task"],
        "The user wants to update an existing task.",
    ),
    (
        "mark_task_done",
        ["done", "complete", "finish task", "mark done"],
        "The user wants to mark a task as completed.",
    ),
    (
        "delete_task",
        ["delete", "remove task"],
        "The user wants to delete a task.",
    ),
]


def decide_tool(message: str) -> tuple[str | None, str]:
    """Pick a tool for the given message based on simple keyword rules.

    Returns a (selected_tool, reason) tuple. selected_tool is None if
    no rule matched.
    """
    lowered_message = message.lower()

    for tool_name, keywords, reason in RULES:
        if any(keyword in lowered_message for keyword in keywords):
            return tool_name, reason

    return None, "No matching tool was found for this message."


# Filler phrases to strip when turning a create_task message into a
# task title, e.g. "Add a task to buy milk" -> "buy milk".
_TASK_TITLE_FILLER_PHRASES = [
    "add a task to",
    "create a task to",
    "new task to",
    "todo",
]


def _extract_task_title(message: str) -> str:
    """Pull a short title out of a create_task message.

    Removes known filler phrases (case-insensitive). If nothing
    meaningful is left afterwards, falls back to the full message.
    """
    title = message
    lowered = message.lower()

    for phrase in _TASK_TITLE_FILLER_PHRASES:
        index = lowered.find(phrase)
        if index != -1:
            title = title[:index] + title[index + len(phrase):]
            lowered = title.lower()

    title = title.strip(" .!?")

    # Extraction was too weak (e.g. the whole message was filler words) -
    # just use the original message as-is.
    if not title:
        return message

    return title


def _extract_city(message: str) -> str | None:
    """Pull a city name out of a weather message.

    Looks for patterns like "weather in London" or "forecast for
    Paris" by taking whatever comes after the last " in " / " for ".
    Returns None if no city could be found.
    """
    cleaned = message.strip().rstrip("?.!")
    lowered = cleaned.lower()

    for keyword in [" in ", " for "]:
        index = lowered.rfind(keyword)
        if index != -1:
            city = cleaned[index + len(keyword):].strip()
            if city:
                return city

    return None


def _task_to_dict(task) -> dict:
    """Convert an internal Task object into a plain JSON-friendly dict."""
    return TaskResponse(
        id=task.id,
        title=task.title,
        description=task.description,
        done=task.done,
    ).model_dump()


def _execute_create_task(message: str) -> dict:
    title = _extract_task_title(message)
    task = task_service.create_task(title=title, description=None)
    return _task_to_dict(task)


def _execute_list_tasks(message: str) -> list[dict]:
    lowered = message.lower()

    if "unfinished" in lowered or "incomplete" in lowered or "not done" in lowered:
        done = False
    elif "completed" in lowered or "done" in lowered:
        done = True
    else:
        done = None

    tasks = task_service.list_tasks(done=done)
    return [_task_to_dict(task) for task in tasks]


def _execute_get_weather(message: str) -> dict:
    city = _extract_city(message)
    if city is None:
        return {"error": "Could not find a city in your message. Please include a city, e.g. 'weather in London'."}

    try:
        return weather_service.get_weather_for_city(city)
    except weather_service.CityNotFoundError:
        return {"error": f"City '{city}' was not found."}
    except weather_service.WeatherServiceError:
        return {"error": "The weather service is currently unavailable."}


def _extract_task_id(message: str) -> int | None:
    """Pull a task id out of a message, e.g. "Mark task 1 as done" -> 1.

    Returns the first run of digits found in the message, or None if
    there are no digits at all.
    """
    match = re.search(r"\d+", message)
    if match is None:
        return None
    return int(match.group())


def _execute_mark_task_done(message: str) -> dict:
    task_id = _extract_task_id(message)
    if task_id is None:
        return {"error": "Could not find a task id in your message. Please include a task id, e.g. 'mark task 1 as done'."}

    task = task_service.mark_task_done(task_id)
    if task is None:
        return {"error": f"Task {task_id} was not found."}

    return _task_to_dict(task)


# Tools that are known but not executable yet - they just report that.
_NOT_IMPLEMENTED_TOOLS = ["update_task", "delete_task"]


def execute_tool(message: str, selected_tool: str | None) -> dict | list | None:
    """Run the tool selected for this message, if execution is supported.

    Returns a JSON-friendly result (dict or list), or None if there is
    no tool to execute.
    """
    if selected_tool == "create_task":
        return _execute_create_task(message)

    if selected_tool == "list_tasks":
        return _execute_list_tasks(message)

    if selected_tool == "get_weather":
        return _execute_get_weather(message)

    if selected_tool == "mark_task_done":
        return _execute_mark_task_done(message)

    if selected_tool in _NOT_IMPLEMENTED_TOOLS:
        return {
            "status": "not_implemented",
            "message": f"Execution for '{selected_tool}' is not implemented yet.",
        }

    return None
