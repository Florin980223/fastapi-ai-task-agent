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
#
# delete_task is checked before create_task on purpose: create_task's
# "todo" keyword would otherwise also match messages like "Delete todo
# 3" (which contains the word "todo"), wrongly creating a task instead
# of deleting one. Putting the more specific "delete"/"remove task"
# keywords first means a message like that is claimed by delete_task
# before create_task ever gets a chance to look at it.
RULES: list[tuple[str, list[str], str]] = [
    (
        "get_weather",
        ["weather", "temperature", "forecast", "city weather"],
        "The user is asking about weather.",
    ),
    (
        "delete_task",
        ["delete", "remove task"],
        "The user wants to delete a task.",
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


def _extract_new_title(message: str) -> str | None:
    """Pull the new title out of an update_task message.

    Looks for "<task id> to <new title>", e.g. "Update task 1 to Call
    Alex tomorrow" -> "Call Alex tomorrow". Anchoring on the digits
    right before "to" (instead of just the first "to" in the message)
    means a title that itself contains "to" (e.g. "task 1 to Talk to
    Bob") is still captured in full. Returns None if no title found.
    """
    match = re.search(r"\d+\s+to\s+(.+)", message, re.IGNORECASE)
    if match is None:
        return None

    title = match.group(1).strip(" .!?")
    if not title:
        return None

    return title


def _execute_update_task(message: str) -> dict:
    task_id = _extract_task_id(message)
    if task_id is None:
        return {"error": "Could not find a task id in your message. Please include a task id, e.g. 'update task 1 to New title'."}

    new_title = _extract_new_title(message)
    if new_title is None:
        return {"error": "Could not find a new title in your message. Please include a new title, e.g. 'update task 1 to New title'."}

    task = task_service.update_task(task_id, title=new_title, description=None)
    if task is None:
        return {"error": f"Task {task_id} was not found."}

    return _task_to_dict(task)


def _execute_delete_task(message: str) -> dict:
    task_id = _extract_task_id(message)
    if task_id is None:
        return {"error": "Could not find a task id in your message. Please include a task id, e.g. 'delete task 1'."}

    # delete_task returns True/False instead of a Task, since the task
    # no longer exists afterwards - there's nothing to convert to a dict.
    deleted = task_service.delete_task(task_id)
    if not deleted:
        return {"error": f"Task {task_id} was not found."}

    return {"status": "deleted", "task_id": task_id}


# Tools that are known but not executable yet - they just report that.
# (Empty for now - every tool in the registry is executable.)
_NOT_IMPLEMENTED_TOOLS: list[str] = []


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

    if selected_tool == "update_task":
        return _execute_update_task(message)

    if selected_tool == "delete_task":
        return _execute_delete_task(message)

    if selected_tool in _NOT_IMPLEMENTED_TOOLS:
        return {
            "status": "not_implemented",
            "message": f"Execution for '{selected_tool}' is not implemented yet.",
        }

    return None


def _describe_task(task: dict) -> str:
    status = "done" if task["done"] else "pending"
    return f'#{task["id"]} "{task["title"]}" ({status})'


def generate_final_answer(selected_tool: str | None, result: dict | list | None) -> str:
    """Turn a tool's result into a short, human-readable sentence.

    This is plain string formatting based on the result shapes that
    execute_tool already produces above - no AI/LLM involved.
    """
    if selected_tool is None:
        return "I couldn't figure out what to do with that message. Could you rephrase it?"

    if isinstance(result, dict) and "error" in result:
        # Error messages built by the _execute_* helpers are already
        # complete, user-friendly sentences - just pass them through.
        return result["error"]

    if isinstance(result, dict) and result.get("status") == "not_implemented":
        return f"Sorry, '{selected_tool}' isn't supported yet."

    if selected_tool == "create_task":
        return f'Created task #{result["id"]}: "{result["title"]}".'

    if selected_tool == "mark_task_done":
        return f'Marked task #{result["id"]} ("{result["title"]}") as done.'

    if selected_tool == "update_task":
        return f'Updated task #{result["id"]} to "{result["title"]}".'

    if selected_tool == "delete_task":
        return f'Deleted task #{result["task_id"]}.'

    if selected_tool == "list_tasks":
        if not result:
            return "You have no tasks."
        task_descriptions = ", ".join(_describe_task(task) for task in result)
        count = len(result)
        noun = "task" if count == 1 else "tasks"
        return f"You have {count} {noun}: {task_descriptions}."

    if selected_tool == "get_weather":
        return f'It\'s currently {result["current_temperature"]}°C in {result["city"]} with wind speed {result["wind_speed"]} km/h.'

    return "Done."
