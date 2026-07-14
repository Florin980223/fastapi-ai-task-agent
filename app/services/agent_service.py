"""Tool execution and final-answer generation for the agent.

This consumes a ToolDecision (see agent_decision.py) - which tool to
run and with what arguments - and actually runs it using the existing
task/weather services. It never looks at the raw user message itself;
that's entirely the decision provider's job. This separation means a
future AI-based decision provider could replace agent_decision.py
without any changes here.
"""

from app.schemas import TaskResponse
from app.services import task_service, weather_service
from app.services.agent_decision import ToolDecision


def _task_to_dict(task) -> dict:
    """Convert an internal Task object into a plain JSON-friendly dict."""
    return TaskResponse(
        id=task.id,
        title=task.title,
        description=task.description,
        done=task.done,
    ).model_dump()


def _execute_create_task(arguments: dict) -> dict:
    task = task_service.create_task(title=arguments["title"], description=None)
    return _task_to_dict(task)


def _execute_list_tasks(arguments: dict) -> list[dict]:
    tasks = task_service.list_tasks(done=arguments.get("done"))
    return [_task_to_dict(task) for task in tasks]


def _execute_get_weather(arguments: dict) -> dict:
    city = arguments.get("city")
    if city is None:
        return {"error": "Could not find a city in your message. Please include a city, e.g. 'weather in London'."}

    try:
        return weather_service.get_weather_for_city(city)
    except weather_service.CityNotFoundError:
        return {"error": f"City '{city}' was not found."}
    except weather_service.WeatherServiceError:
        return {"error": "The weather service is currently unavailable."}


def _execute_mark_task_done(arguments: dict) -> dict:
    task_id = arguments.get("task_id")
    if task_id is None:
        return {"error": "Could not find a task id in your message. Please include a task id, e.g. 'mark task 1 as done'."}

    task = task_service.mark_task_done(task_id)
    if task is None:
        return {"error": f"Task {task_id} was not found."}

    return _task_to_dict(task)


def _execute_update_task(arguments: dict) -> dict:
    task_id = arguments.get("task_id")
    if task_id is None:
        return {"error": "Could not find a task id in your message. Please include a task id, e.g. 'update task 1 to New title'."}

    new_title = arguments.get("title")
    if new_title is None:
        return {"error": "Could not find a new title in your message. Please include a new title, e.g. 'update task 1 to New title'."}

    task = task_service.update_task(task_id, title=new_title, description=None)
    if task is None:
        return {"error": f"Task {task_id} was not found."}

    return _task_to_dict(task)


def _execute_delete_task(arguments: dict) -> dict:
    task_id = arguments.get("task_id")
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


def execute_tool(decision: ToolDecision) -> dict | list | None:
    """Run the tool selected by a decision, if execution is supported.

    Returns a JSON-friendly result (dict or list), or None if there is
    no tool to execute.
    """
    selected_tool = decision.selected_tool
    arguments = decision.arguments

    if selected_tool == "create_task":
        return _execute_create_task(arguments)

    if selected_tool == "list_tasks":
        return _execute_list_tasks(arguments)

    if selected_tool == "get_weather":
        return _execute_get_weather(arguments)

    if selected_tool == "mark_task_done":
        return _execute_mark_task_done(arguments)

    if selected_tool == "update_task":
        return _execute_update_task(arguments)

    if selected_tool == "delete_task":
        return _execute_delete_task(arguments)

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
