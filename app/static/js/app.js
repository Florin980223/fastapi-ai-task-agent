import * as api from "./api.js";

// ---------------------------------------------------------------------
// Toast / status helpers
// ---------------------------------------------------------------------

const toastEl = document.getElementById("toast");
let toastTimer = null;

function showToast(message) {
  toastEl.textContent = message;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastEl.textContent = "";
  }, 6000);
}

function setStatus(el, message, tone) {
  el.textContent = message || "";
  if (tone) {
    el.dataset.tone = tone;
  } else {
    delete el.dataset.tone;
  }
}

function describeError(err) {
  // Backend-supplied string details are already complete, human-friendly
  // sentences (see app/services/agent_service.py's _execute_* helpers) -
  // pass those through unchanged. Anything else (a non-string detail like
  // a raw validation-error object, or no detail at all) never gets shown
  // to the user as JSON - only a plain, generic sentence for that status.
  if (err && err.name === "ApiError") {
    if (typeof err.detail === "string" && err.detail.trim()) {
      return err.detail;
    }
    if (err.status === 401) {
      return "Your session isn't valid. Please sign in again.";
    }
    if (err.status === 429) {
      return "You're sending requests too quickly. Please slow down and try again shortly.";
    }
    if (err.status >= 500) {
      return "Something went wrong on our end. Please try again.";
    }
    return "That request couldn't be processed. Please check your input and try again.";
  }
  return "Something went wrong. Please try again.";
}

// ---------------------------------------------------------------------
// API-key gate
// ---------------------------------------------------------------------

const apiKeyGate = document.getElementById("apiKeyGate");
const appShell = document.getElementById("appShell");
const apiKeyForm = document.getElementById("apiKeyForm");
const apiKeyInput = document.getElementById("apiKeyInput");
const apiKeyGateError = document.getElementById("apiKeyGateError");
const keyStatusText = document.getElementById("keyStatusText");
const clearKeyBtn = document.getElementById("clearKeyBtn");

function showSignedIn() {
  apiKeyGate.hidden = true;
  appShell.hidden = false;
  keyStatusText.textContent = "Signed in";
  clearKeyBtn.hidden = false;
}

function showSignedOut() {
  appShell.hidden = true;
  apiKeyGate.hidden = false;
  keyStatusText.textContent = "Not signed in";
  clearKeyBtn.hidden = true;
  apiKeyInput.value = "";
  apiKeyInput.focus();
}

apiKeyForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = apiKeyInput.value.trim();
  if (!value) {
    apiKeyGateError.hidden = false;
    apiKeyGateError.textContent = "Enter a key before continuing.";
    return;
  }
  apiKeyGateError.hidden = true;
  api.setApiKey(value);
  showSignedIn();
  loadTasks();
  loadRuns();
});

clearKeyBtn.addEventListener("click", () => {
  api.clearApiKey();
  showToast("API key cleared from this browser tab.");
  showSignedOut();
});

function handleUnauthorized() {
  api.clearApiKey();
  showToast("Your API key was rejected. Please sign in again.");
  showSignedOut();
}

if (api.getApiKey()) {
  showSignedIn();
} else {
  showSignedOut();
}

// ---------------------------------------------------------------------
// Accessible tabs (WAI-ARIA APG, automatic activation)
// ---------------------------------------------------------------------

const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
const panels = {
  "tab-tasks": document.getElementById("panel-tasks"),
  "tab-agent": document.getElementById("panel-agent"),
  "tab-history": document.getElementById("panel-history"),
};

function activateTab(tab) {
  tabs.forEach((t) => {
    const selected = t === tab;
    t.setAttribute("aria-selected", String(selected));
    t.tabIndex = selected ? 0 : -1;
    panels[t.id].hidden = !selected;
  });
  tab.focus();
  if (tab.id === "tab-history") {
    loadRuns();
  }
}

tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => activateTab(tab));
  tab.addEventListener("keydown", (event) => {
    let targetIndex = null;
    if (event.key === "ArrowRight") targetIndex = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") targetIndex = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") targetIndex = 0;
    else if (event.key === "End") targetIndex = tabs.length - 1;
    if (targetIndex !== null) {
      event.preventDefault();
      activateTab(tabs[targetIndex]);
    }
  });
});

// ---------------------------------------------------------------------
// Tasks panel
// ---------------------------------------------------------------------

const tasksStatus = document.getElementById("tasksStatus");
const taskList = document.getElementById("taskList");
const taskCreateForm = document.getElementById("taskCreateForm");
const taskTitleInput = document.getElementById("taskTitleInput");
const taskDescriptionInput = document.getElementById("taskDescriptionInput");
const taskPriorityInput = document.getElementById("taskPriorityInput");
const taskDueDateInput = document.getElementById("taskDueDateInput");
const taskCreateSubmit = document.getElementById("taskCreateSubmit");

const confirmDialog = document.getElementById("confirmDialog");
const confirmDialogBody = document.getElementById("confirmDialogBody");
const confirmDialogCancel = document.getElementById("confirmDialogCancel");
const confirmDialogConfirm = document.getElementById("confirmDialogConfirm");
let pendingDeleteTaskId = null;
let pendingDeleteTrigger = null;

const PRIORITY_LABELS = { low: "Low", medium: "Medium", high: "High" };

// due_date is a calendar date only ("YYYY-MM-DD"), never a timestamp -
// deliberately never parsed via `new Date("YYYY-MM-DD")` (parsed as UTC
// midnight by the JS spec, which can then render as the previous day in
// any timezone behind UTC) or any other UTC-based conversion. Splitting
// the components and using the *local-time* Date constructor
// (new Date(year, monthIndex, day)) means the calendar day displayed is
// always exactly the day stored, regardless of the viewer's timezone.
function formatDueDate(isoDate) {
  const [year, month, day] = isoDate.split("-").map(Number);
  const local = new Date(year, month - 1, day);
  return local.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

function renderTasks(tasks) {
  taskList.innerHTML = "";
  if (tasks.length === 0) {
    setStatus(tasksStatus, "No tasks yet — add one above.", null);
    return;
  }
  setStatus(tasksStatus, "", null);

  for (const task of tasks) {
    const li = document.createElement("li");
    li.className = "task-item";
    li.dataset.taskId = String(task.id);
    li.dataset.done = String(task.done);

    const info = document.createElement("div");
    const title = document.createElement("p");
    title.className = "task-title";
    title.textContent = task.title;
    info.appendChild(title);
    if (task.description) {
      const desc = document.createElement("p");
      desc.className = "task-description";
      desc.textContent = task.description;
      info.appendChild(desc);
    }

    const meta = document.createElement("div");
    meta.className = "task-meta";

    const priorityBadge = document.createElement("span");
    priorityBadge.className = "priority-badge";
    priorityBadge.dataset.priority = task.priority;
    priorityBadge.textContent = PRIORITY_LABELS[task.priority] || task.priority;
    meta.appendChild(priorityBadge);

    if (task.due_date) {
      const due = document.createElement("span");
      due.className = "task-due-date";
      due.textContent = `Due ${formatDueDate(task.due_date)}`;
      meta.appendChild(due);
    }

    info.appendChild(meta);
    li.appendChild(info);

    const actions = document.createElement("div");
    actions.className = "task-actions";

    const doneBtn = document.createElement("button");
    doneBtn.type = "button";
    doneBtn.className = "btn btn-secondary";
    doneBtn.textContent = task.done ? "Mark not done" : "Mark done";
    doneBtn.disabled = task.done;
    doneBtn.addEventListener("click", () => onMarkDone(task.id));
    actions.appendChild(doneBtn);

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "btn btn-secondary";
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", () => onEditTask(li, task));
    actions.appendChild(editBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "btn btn-danger";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", () => onRequestDelete(task, deleteBtn));
    actions.appendChild(deleteBtn);

    li.appendChild(actions);
    taskList.appendChild(li);
  }
}

async function loadTasks() {
  setStatus(tasksStatus, "Loading tasks…", null);
  taskList.innerHTML = "";
  try {
    const tasks = await api.listTasks();
    renderTasks(tasks);
  } catch (err) {
    if (err.status === 401) return handleUnauthorized();
    setStatus(tasksStatus, describeError(err), "error");
  }
}

taskCreateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = taskTitleInput.value.trim();
  if (!title) return;
  taskCreateSubmit.disabled = true;
  try {
    await api.createTask({
      title,
      description: taskDescriptionInput.value.trim(),
      priority: taskPriorityInput.value,
      dueDate: taskDueDateInput.value,
    });
    taskCreateForm.reset();
    showToast("Task added.");
    await loadTasks();
  } catch (err) {
    if (err.status === 401) return handleUnauthorized();
    setStatus(tasksStatus, describeError(err), "error");
  } finally {
    taskCreateSubmit.disabled = false;
  }
});

async function onMarkDone(taskId) {
  try {
    await api.markTaskDone(taskId);
    await loadTasks();
  } catch (err) {
    if (err.status === 401) return handleUnauthorized();
    showToast(describeError(err));
  }
}

function onEditTask(li, task) {
  li.innerHTML = "";
  const form = document.createElement("form");
  form.className = "task-edit-form";

  const titleField = document.createElement("input");
  titleField.type = "text";
  titleField.value = task.title;
  titleField.maxLength = 200;
  titleField.required = true;
  titleField.setAttribute("aria-label", "Title");

  const descField = document.createElement("textarea");
  descField.value = task.description || "";
  descField.maxLength = 2000;
  descField.rows = 2;
  descField.setAttribute("aria-label", "Description");

  const priorityField = document.createElement("select");
  for (const value of ["low", "medium", "high"]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = PRIORITY_LABELS[value];
    if (value === task.priority) option.selected = true;
    priorityField.appendChild(option);
  }
  priorityField.setAttribute("aria-label", "Priority");

  const dueDateField = document.createElement("input");
  dueDateField.type = "date";
  dueDateField.value = task.due_date || "";
  dueDateField.setAttribute("aria-label", "Due date");

  const actions = document.createElement("div");
  actions.className = "task-actions";

  const saveBtn = document.createElement("button");
  saveBtn.type = "submit";
  saveBtn.className = "btn btn-primary";
  saveBtn.textContent = "Save";

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "btn btn-secondary";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => loadTasks());

  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);
  form.appendChild(titleField);
  form.appendChild(descField);
  form.appendChild(priorityField);
  form.appendChild(dueDateField);
  form.appendChild(actions);
  li.appendChild(form);
  titleField.focus();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    saveBtn.disabled = true;
    try {
      // Always sends all four fields (present value or null for a
      // cleared due date) - same "the edit form resends the whole
      // editable state on save" convention title/description already
      // use, which is exactly what exercises the due_date "explicit
      // null clears it" behavior without needing to detect whether the
      // user actually touched the field.
      await api.updateTask(task.id, {
        title: titleField.value.trim(),
        description: descField.value.trim(),
        priority: priorityField.value,
        dueDate: dueDateField.value,
      });
      showToast("Task updated.");
      await loadTasks();
    } catch (err) {
      if (err.status === 401) return handleUnauthorized();
      showToast(describeError(err));
      saveBtn.disabled = false;
    }
  });
}

function onRequestDelete(task, trigger) {
  pendingDeleteTaskId = task.id;
  pendingDeleteTrigger = trigger;
  confirmDialogBody.textContent = `Delete "${task.title}"? This cannot be undone.`;
  confirmDialog.showModal();
}

confirmDialogCancel.addEventListener("click", () => {
  confirmDialog.close();
});

confirmDialog.addEventListener("close", () => {
  if (pendingDeleteTrigger) {
    pendingDeleteTrigger.focus();
    pendingDeleteTrigger = null;
  }
});

confirmDialogConfirm.addEventListener("click", async () => {
  const taskId = pendingDeleteTaskId;
  confirmDialogConfirm.disabled = true;
  try {
    await api.deleteTask(taskId);
    confirmDialog.close();
    showToast("Task deleted.");
    await loadTasks();
  } catch (err) {
    confirmDialog.close();
    if (err.status === 401) return handleUnauthorized();
    showToast(describeError(err));
  } finally {
    confirmDialogConfirm.disabled = false;
  }
});

// ---------------------------------------------------------------------
// Agent panel
// ---------------------------------------------------------------------

const agentForm = document.getElementById("agentForm");
const agentMessageInput = document.getElementById("agentMessageInput");
const agentMessageLabel = document.getElementById("agentMessageLabel");
const agentSubmit = document.getElementById("agentSubmit");
const agentStatus = document.getElementById("agentStatus");
const agentResult = document.getElementById("agentResult");
const agentNewConversationBtn = document.getElementById("agentNewConversationBtn");
const agentHint = document.getElementById("agentHint");

let currentConversationId = null;
let agentRequestInFlight = false;

function resetConversation() {
  currentConversationId = null;
  agentMessageLabel.textContent = "Message";
  agentHint.textContent = 'Describe what you want in plain language, e.g. "Add a task to buy milk" or "Delete task 3".';
  agentMessageInput.value = "";
  agentResult.innerHTML = "";
  setStatus(agentStatus, "", null);
}

agentNewConversationBtn.addEventListener("click", () => {
  resetConversation();
  agentMessageInput.focus();
});

// Clean, non-technical headline for each tool's *successful* result. Only
// reached when response.result has no "error" key - see renderAgentResponse.
const TOOL_SUCCESS_HEADLINES = {
  create_task: "Task created",
  mark_task_done: "Task completed",
  update_task: "Task updated",
  delete_task: "Task deleted",
};

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function appendHeadline(container, text) {
  const heading = document.createElement("h3");
  heading.className = "agent-headline";
  heading.textContent = text;
  container.appendChild(heading);
  return heading;
}

function appendDetail(container, text, tone) {
  if (!text) return null;
  const detail = document.createElement("p");
  detail.className = "agent-detail";
  if (tone) detail.dataset.tone = tone;
  detail.textContent = text;
  container.appendChild(detail);
  return detail;
}

// Collapsed-by-default technical/debugging information: internal tool
// name, run id, backend reason, raw result JSON, and multi-step data.
// Never shown by default - only on explicit user disclosure.
function appendTechnicalDetails(container, response) {
  const details = document.createElement("details");
  details.className = "agent-technical-details";

  const summary = document.createElement("summary");
  summary.textContent = "Technical details";
  details.appendChild(summary);

  const fields = document.createElement("dl");
  const addField = (term, value) => {
    if (value === null || value === undefined || value === "") return;
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = value;
    fields.appendChild(dt);
    fields.appendChild(dd);
  };
  addField("Tool", response.selected_tool);
  addField("Run ID", response.run_id);
  addField("Reason", response.reason);
  if (fields.childElementCount > 0) details.appendChild(fields);

  if (response.result !== null && response.result !== undefined) {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(response.result, null, 2);
    details.appendChild(pre);
  }

  if (response.is_multi_step && response.steps && response.steps.length > 0) {
    const stepList = document.createElement("ol");
    stepList.className = "agent-step-list";
    for (const step of response.steps) {
      const li = document.createElement("li");
      li.textContent = `Step ${step.step}: ${step.tool} — ${step.status}` + (step.error ? ` (${step.error})` : "");
      stepList.appendChild(li);
    }
    details.appendChild(stepList);
  }

  container.appendChild(details);
}

function renderAgentResponse(response) {
  agentResult.innerHTML = "";

  const resultError = isPlainObject(response.result) && typeof response.result.error === "string" ? response.result.error : null;

  if (resultError) {
    // selected_tool may still be set here (e.g. a stale/expired
    // confirmation) - a result carrying an error is never treated as a
    // tool-specific success, regardless of which tool was selected.
    appendHeadline(agentResult, "Couldn't complete that");
    appendDetail(agentResult, resultError, "error");
  } else if (response.needs_clarification && response.clarification_options && response.clarification_options.length > 0) {
    appendHeadline(agentResult, "I found multiple matching tasks.");
    appendDetail(agentResult, "Which one did you mean?");

    const options = document.createElement("div");
    options.className = "agent-options";
    for (const option of response.clarification_options) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-secondary";
      button.textContent = option.title;
      // Sends the unambiguous numeric id - never guesses by title text.
      button.addEventListener("click", () => sendAgentMessage(String(option.task_id)));
      options.appendChild(button);
    }
    agentResult.appendChild(options);
  } else if (response.needs_clarification) {
    // No candidates to choose from (missing argument, or no title match
    // at all) - the backend's question is already a plain, human sentence.
    appendHeadline(agentResult, response.clarification_question);
  } else if (response.needs_confirmation) {
    // Rendered exactly as the backend sent it - never parsed/reconstructed
    // client-side. Confirm/Cancel just send the same "yes"/"no" replies a
    // typed response already would.
    appendHeadline(agentResult, response.confirmation_question);

    const actions = document.createElement("div");
    actions.className = "agent-confirm-actions";
    const confirmBtn = document.createElement("button");
    confirmBtn.type = "button";
    confirmBtn.className = "btn btn-danger";
    confirmBtn.textContent = "Confirm";
    confirmBtn.addEventListener("click", () => sendAgentMessage("yes"));
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-secondary";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", () => sendAgentMessage("no"));
    actions.appendChild(confirmBtn);
    actions.appendChild(cancelBtn);
    agentResult.appendChild(actions);
  } else if (response.selected_tool === "update_task") {
    // updated_fields (which mutable field(s) this request actually
    // touched) drives the headline precisely - never inferred from
    // result.due_date alone, which can't tell "just cleared" apart from
    // "never had one and wasn't touched this request" (see
    // ExecuteResponse.updated_fields's docstring in app/schemas.py).
    const result = isPlainObject(response.result) ? response.result : {};
    const updatedFields = new Set(response.updated_fields || []);
    const newTitle = typeof result.title === "string" ? result.title : null;

    if (updatedFields.size === 1 && updatedFields.has("priority")) {
      appendHeadline(agentResult, "Priority updated");
      appendDetail(agentResult, newTitle);
      if (result.priority) appendDetail(agentResult, `${PRIORITY_LABELS[result.priority] || result.priority} priority`);
    } else if (updatedFields.size === 1 && updatedFields.has("due_date")) {
      appendHeadline(agentResult, result.due_date ? "Deadline updated" : "Deadline cleared");
      appendDetail(agentResult, newTitle);
      if (result.due_date) appendDetail(agentResult, `Due ${formatDueDate(result.due_date)}`);
    } else {
      // Title changed (alone or combined with priority/due_date) - the
      // general case, same title-arrow presentation as before.
      appendHeadline(agentResult, "Task updated");
      if (response.resolved_task_title && newTitle) {
        appendDetail(agentResult, `${response.resolved_task_title} → ${newTitle}`);
      } else if (newTitle) {
        appendDetail(agentResult, newTitle);
      } else if (response.resolved_task_title) {
        appendDetail(agentResult, response.resolved_task_title);
      }
      const meta = [];
      if (updatedFields.has("priority") && result.priority) {
        meta.push(`${PRIORITY_LABELS[result.priority] || result.priority} priority`);
      }
      if (updatedFields.has("due_date")) {
        meta.push(result.due_date ? `Due ${formatDueDate(result.due_date)}` : "Deadline cleared");
      }
      if (meta.length > 0) appendDetail(agentResult, meta.join(" · "));
    }
  } else if (response.selected_tool && TOOL_SUCCESS_HEADLINES[response.selected_tool]) {
    appendHeadline(agentResult, TOOL_SUCCESS_HEADLINES[response.selected_tool]);

    const newTitle = isPlainObject(response.result) && typeof response.result.title === "string" ? response.result.title : null;
    if (newTitle) {
      appendDetail(agentResult, newTitle);
    } else if (response.resolved_task_title) {
      appendDetail(agentResult, response.resolved_task_title);
    } else if (isPlainObject(response.result) && response.result.task_id !== undefined) {
      appendDetail(agentResult, `Task #${response.result.task_id}`);
    }

    if (response.selected_tool === "create_task" && isPlainObject(response.result)) {
      // Only shown when it's informative - a plain default-priority,
      // no-due-date task keeps the existing clean two-line look.
      const meta = [];
      if (response.result.priority && response.result.priority !== "medium") {
        meta.push(`${PRIORITY_LABELS[response.result.priority] || response.result.priority} priority`);
      }
      if (response.result.due_date) {
        meta.push(`Due ${formatDueDate(response.result.due_date)}`);
      }
      if (meta.length > 0) appendDetail(agentResult, meta.join(" · "));
    }
  } else {
    // list_tasks, get_weather, not_implemented, unknown intent - the
    // backend's final_answer is already clean, human-readable prose.
    appendHeadline(agentResult, response.final_answer);
  }

  appendTechnicalDetails(agentResult, response);
}

function enterPendingReplyMode(question) {
  agentMessageLabel.textContent = "Your reply";
  agentHint.textContent = question;
  agentMessageInput.value = "";
}

// Shared by the form's own submit, the Confirm/Cancel buttons, and the
// clarification-option buttons - all three are just different ways of
// sending a message into the same conversation, with identical
// in-flight/status/error/task-refresh handling.
async function sendAgentMessage(message) {
  if (agentRequestInFlight) return;

  agentRequestInFlight = true;
  agentSubmit.disabled = true;
  setStatus(agentStatus, "Working…", null);

  try {
    const response = await api.executeAgent({ message, conversationId: currentConversationId });
    currentConversationId = response.conversation_id;
    renderAgentResponse(response);

    if (response.needs_clarification) {
      enterPendingReplyMode(response.clarification_question);
      setStatus(agentStatus, "Waiting for clarification.", null);
    } else if (response.needs_confirmation) {
      enterPendingReplyMode(response.confirmation_question);
      setStatus(agentStatus, "Waiting for confirmation.", null);
    } else {
      agentMessageLabel.textContent = "Message";
      agentHint.textContent = 'Describe what you want in plain language, e.g. "Add a task to buy milk" or "Delete task 3".';
      agentMessageInput.value = "";
      setStatus(agentStatus, "Done.", "success");
      loadTasks();
    }
  } catch (err) {
    if (err.status === 401) return handleUnauthorized();
    setStatus(agentStatus, describeError(err), "error");
  } finally {
    agentRequestInFlight = false;
    agentSubmit.disabled = false;
    agentMessageInput.focus();
  }
}

agentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = agentMessageInput.value.trim();
  if (!message) return;
  await sendAgentMessage(message);
});

// ---------------------------------------------------------------------
// History panel
// ---------------------------------------------------------------------

const historyStatus = document.getElementById("historyStatus");
const runList = document.getElementById("runList");
const runDetail = document.getElementById("runDetail");
const historyRefreshBtn = document.getElementById("historyRefreshBtn");

function formatTimestamp(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

async function loadRuns() {
  setStatus(historyStatus, "Loading runs…", null);
  runList.innerHTML = "";
  runDetail.innerHTML = "";
  try {
    const runs = await api.listRuns(20);
    if (runs.length === 0) {
      setStatus(historyStatus, "No runs yet — try the Agent tab.", null);
      return;
    }
    setStatus(historyStatus, "", null);
    for (const run of runs) {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = "";

      const statusPill = document.createElement("span");
      statusPill.className = "status-pill";
      statusPill.textContent = run.status;

      button.appendChild(statusPill);
      button.appendChild(document.createTextNode(` ${run.selected_tool || "(no tool)"} — ${formatTimestamp(run.started_at)}`));
      button.addEventListener("click", () => showRunDetail(run.run_id, button));

      li.appendChild(button);
      runList.appendChild(li);
    }
  } catch (err) {
    if (err.status === 401) return handleUnauthorized();
    setStatus(historyStatus, describeError(err), "error");
  }
}

async function showRunDetail(runId, triggerButton) {
  Array.from(runList.querySelectorAll("button")).forEach((btn) => btn.removeAttribute("aria-current"));
  triggerButton.setAttribute("aria-current", "true");

  runDetail.textContent = "Loading run…";
  try {
    const run = await api.getRun(runId);
    runDetail.innerHTML = "";

    const heading = document.createElement("h3");
    heading.textContent = `Run ${run.run_id}`;
    runDetail.appendChild(heading);

    const summary = document.createElement("p");
    summary.textContent = `Status: ${run.status} · Tool: ${run.selected_tool || "(none)"} · Started: ${formatTimestamp(run.started_at)} · Duration: ${run.duration_ms}ms`;
    runDetail.appendChild(summary);

    const message = document.createElement("p");
    message.textContent = `Message: "${run.message}"`;
    runDetail.appendChild(message);

    if (run.error) {
      const error = document.createElement("p");
      error.dataset.tone = "error";
      error.textContent = `Error: ${run.error}`;
      runDetail.appendChild(error);
    }

    if (run.steps && run.steps.length > 0) {
      const stepList = document.createElement("ol");
      stepList.className = "agent-step-list";
      for (const step of run.steps) {
        const li = document.createElement("li");
        li.textContent = `Step ${step.step_number}: ${step.tool} — ${step.status}` + (step.error ? ` (${step.error})` : "");
        stepList.appendChild(li);
      }
      runDetail.appendChild(stepList);
    }
  } catch (err) {
    if (err.status === 401) return handleUnauthorized();
    runDetail.textContent = describeError(err);
  }
}

historyRefreshBtn.addEventListener("click", loadRuns);

// ---------------------------------------------------------------------
// Initial load
// ---------------------------------------------------------------------

if (api.getApiKey()) {
  loadTasks();
  loadRuns();
}
