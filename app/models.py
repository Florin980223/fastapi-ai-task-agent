"""In-memory data model and storage for tasks.

There is no database yet: tasks live in a plain Python list that
resets every time the server restarts. This keeps the first version
of the project simple while the API shape is worked out.
"""


class Task:
    """A single task."""

    def __init__(self, id: int, title: str, description: str | None = None, done: bool = False):
        self.id = id
        self.title = title
        self.description = description
        self.done = done


# The "database": a list of Task objects, all held in memory.
tasks_db: list[Task] = []

# Simple counter used to give each new task a unique id.
# We don't reuse ids after deletion, so ids stay stable while a task exists.
_next_id = 1


def get_next_id() -> int:
    global _next_id
    new_id = _next_id
    _next_id += 1
    return new_id
