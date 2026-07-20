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
  if (err && err.name === "ApiError") {
    const parts = [];
    if (typeof err.detail === "string") {
      parts.push(err.detail);
    } else if (err.detail) {
      parts.push(JSON.stringify(err.detail));
    } else {
      parts.push("Request failed.");
    }
    if (err.status === 429) {
      parts.push("Please slow down and try again shortly.");
    }
    if (err.requestId) {
      parts.push(`(Request ID: ${err.requestId})`);
    }
    return parts.join(" ");
  }
  return "Something went wrong.";
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
const taskCreateSubmit = document.getElementById("taskCreateSubmit");

const confirmDialog = document.getElementById("confirmDialog");
const confirmDialogBody = document.getElementById("confirmDialogBody");
const confirmDialogCancel = document.getElementById("confirmDialogCancel");
const confirmDialogConfirm = document.getElementById("confirmDialogConfirm");
let pendingDeleteTaskId = null;
let pendingDeleteTrigger = null;

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
    await api.createTask({ title, description: taskDescriptionInput.value.trim() });
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
  form.appendChild(actions);
  li.appendChild(form);
  titleField.focus();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    saveBtn.disabled = true;
    try {
      await api.updateTask(task.id, { title: titleField.value.trim(), description: descField.value.trim() });
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

function renderAgentResponse(response) {
  agentResult.innerHTML = "";

  const answer = document.createElement("p");
  answer.className = "agent-answer";
  answer.textContent = response.final_answer;
  agentResult.appendChild(answer);

  if (response.selected_tool) {
    const tool = document.createElement("p");
    tool.textContent = `Tool selected: ${response.selected_tool}`;
    agentResult.appendChild(tool);
  }

  if (response.result !== null && response.result !== undefined) {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(response.result, null, 2);
    agentResult.appendChild(pre);
  }

  if (response.is_multi_step && response.steps && response.steps.length > 0) {
    const stepList = document.createElement("ol");
    stepList.className = "agent-step-list";
    for (const step of response.steps) {
      const li = document.createElement("li");
      li.textContent = `Step ${step.step}: ${step.tool} — ${step.status}` + (step.error ? ` (${step.error})` : "");
      stepList.appendChild(li);
    }
    agentResult.appendChild(stepList);
  }

  const runInfo = document.createElement("p");
  runInfo.className = "request-id";
  runInfo.textContent = `Run ID: ${response.run_id}`;
  agentResult.appendChild(runInfo);
}

function enterPendingReplyMode(question) {
  agentMessageLabel.textContent = "Your reply";
  agentHint.textContent = question;
  agentMessageInput.value = "";
}

agentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (agentRequestInFlight) return;

  const message = agentMessageInput.value.trim();
  if (!message) return;

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
