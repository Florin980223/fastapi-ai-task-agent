// Thin wrapper around the existing backend API. No business logic lives
// here or anywhere else in this UI - every decision (whether a
// clarification/confirmation is needed, whether a tool is destructive,
// how a task is scoped to a user) is made by the backend; this module
// only forwards requests and normalizes responses/errors.

const SESSION_KEY = "taskAgentApiKey";

export function getApiKey() {
  return sessionStorage.getItem(SESSION_KEY);
}

export function setApiKey(key) {
  sessionStorage.setItem(SESSION_KEY, key);
}

export function clearApiKey() {
  sessionStorage.removeItem(SESSION_KEY);
}

export class ApiError extends Error {
  constructor(status, detail, requestId) {
    super(typeof detail === "string" ? detail : "Request failed");
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.requestId = requestId;
  }
}

async function apiFetch(path, options = {}) {
  const apiKey = getApiKey();
  const headers = new Headers(options.headers || {});
  if (apiKey) {
    headers.set("X-API-Key", apiKey);
  }
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check your connection and try again.", null);
  }

  const requestId = response.headers.get("X-Request-ID");

  if (response.status === 204) {
    return null;
  }

  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload ? payload.detail : payload;
    throw new ApiError(response.status, detail, requestId);
  }

  return payload;
}

// --- Tasks -------------------------------------------------------------

export function listTasks() {
  return apiFetch("/tasks");
}

export function createTask({ title, description, priority, dueDate }) {
  const body = { title, description: description || null };
  if (priority !== undefined) body.priority = priority;
  // An empty date input means "no due date" - never sent as an empty
  // string (which the backend's date parser would reject).
  body.due_date = dueDate || null;
  return apiFetch("/tasks", { method: "POST", body: JSON.stringify(body) });
}

export function updateTask(taskId, { title, description, priority, dueDate }) {
  const body = {};
  if (title !== undefined) body.title = title;
  if (description !== undefined) body.description = description;
  if (priority !== undefined) body.priority = priority;
  // dueDate === undefined means "the caller didn't touch this" - omit
  // the key entirely, so the backend's tri-state (omitted = unchanged)
  // applies. dueDate === null/"" means "clear it" - send null
  // explicitly, distinct from omitting the key.
  if (dueDate !== undefined) body.due_date = dueDate || null;
  return apiFetch(`/tasks/${encodeURIComponent(taskId)}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function markTaskDone(taskId) {
  return apiFetch(`/tasks/${encodeURIComponent(taskId)}/done`, { method: "PATCH" });
}

export function deleteTask(taskId) {
  return apiFetch(`/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

// --- Agent ---------------------------------------------------------------

export function executeAgent({ message, conversationId }) {
  const body = { message };
  if (conversationId) body.conversation_id = conversationId;
  return apiFetch("/agent/execute", { method: "POST", body: JSON.stringify(body) });
}

export function listRuns(limit = 20) {
  return apiFetch(`/agent/runs?limit=${encodeURIComponent(limit)}`);
}

export function getRun(runId) {
  return apiFetch(`/agent/runs/${encodeURIComponent(runId)}`);
}
