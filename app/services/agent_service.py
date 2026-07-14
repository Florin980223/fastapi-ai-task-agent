"""Very simple rule-based "tool selection" for user messages.

This does NOT call any AI/LLM. It just checks the message text for a
few keywords, in order, and picks the first tool whose keywords match.
This is a stand-in for a future version where an actual AI model
decides which tool to call.
"""

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
