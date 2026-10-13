const BRIDGE_URL = 'http://127.0.0.1:8765';
let bridgeToken = '';

type MemoryView = {
  id: number;
  content: string;
  category: string;
  importance: number;
  confidence: number;
  summary: string;
  updated_at: string;
};

type TaskView = {
  id: string;
  goal: string;
  status: string;
  progress: number;
  session_id?: string | null;
  plan?: Array<{ title: string; status: string }>;
};

type ContextView = {
  goal?: string;
  summary?: string;
  session_id?: string;
  history?: Array<{ role: string; content: string }>;
  memories?: MemoryView[];
  unresolved_references?: Array<{ phrase: string; value: string }>;
};

type ExecutionStepView = {
  step_id: string;
  title: string;
  tool: string;
  status: string;
  result: string;
  error: string;
  retry_count: number;
  started_at?: string | null;
  completed_at?: string | null;
};

type ExecutionView = {
  execution_id: string;
  task_id: string;
  goal: string;
  status: string;
  current_step: string;
  result: string;
  error: string;
  steps: ExecutionStepView[];
  created_at: string;
  updated_at: string;
};

type ScreenView = {
  application?: string;
  window?: string;
  text?: string;
  buttons?: string[];
  dialogs?: string[];
  errors?: string[];
  confidence?: number;
};

type SystemView = {
  cpu_count?: number | null;
  memory_percent?: number | null;
  disk_free_gb?: number | null;
  services?: Record<string, boolean>;
  raw?: Record<string, unknown>;
};

type WorkflowStepView = {
  id: string;
  title: string;
  status?: string;
  tool?: string;
};

type WorkflowView = {
  workflow_id: string;
  name: string;
  status: string;
  steps: WorkflowStepView[];
};

type TraceView = {
  id: number;
  event_type: string;
  tool: string;
  model: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
  error: string;
  created_at: string;
};

type HealthView = {
  status: string;
  components: Array<{ name: string; status: string; message?: string }>;
};

type PermissionCenterView = {
  categories: Array<{
    name: string;
    key: string;
    mode: string;
    options: string[];
  }>;
};

type ProductionOverview = {
  memory_count: number;
  task_count: number;
  active_task_count: number;
  conversation_count: number;
  message_count: number;
  backup_count: number;
  cache_size_bytes: number;
  database_size_bytes: number;
  log: { files: number; size_bytes: number; level: string };
  latest_backup?: { name: string; created_at: string } | null;
};

type DiagnosticReport = {
  generated_at: string;
  version: string;
  environment: string;
  status: string;
  findings: string[];
  checks: Record<string, unknown>;
};

type UpdateView = {
  status: string;
  current_version: string;
  available: { version: string; notes: string; channel: string } | null;
  signature_verified: boolean;
  last_error: string;
};

export function memoryPanelRows(memories: MemoryView[]): string[] {
  return memories.map(
    (memory) =>
      `${memory.category} · ${Math.round(memory.confidence * 100)}% · ${memory.content}`
  );
}

export function taskPanelRows(tasks: TaskView[]): string[] {
  return tasks.map(
    (task) =>
      `${task.status} · ${Math.round(task.progress * 100)}% · ${task.goal}`
  );
}

export function contextPanelRows(context: ContextView): string[] {
  const rows: string[] = [];
  if (context.goal) rows.push(`目标: ${context.goal}`);
  if (context.summary) rows.push(`摘要: ${context.summary}`);
  for (const reference of context.unresolved_references || []) {
    rows.push(`引用: ${reference.phrase} -> ${reference.value}`);
  }
  return rows;
}

export function executionPanelRows(execution: ExecutionView): string[] {
  return execution.steps.map(
    (step) =>
      `${step.status} · ${step.title} · ${step.tool || 'NO TOOL'} · retry=${step.retry_count}`
  );
}

export function screenPanelRows(screen: ScreenView): string[] {
  const rows = [
    `APPLICATION: ${screen.application || 'unknown'}`,
    `WINDOW: ${screen.window || 'unknown'}`,
    `CONFIDENCE: ${Math.round((screen.confidence || 0) * 100)}%`
  ];
  if (screen.errors?.length) rows.push(`ERRORS: ${screen.errors.join(', ')}`);
  if (screen.dialogs?.length) rows.push(`DIALOGS: ${screen.dialogs.join(', ')}`);
  if (screen.buttons?.length) rows.push(`BUTTONS: ${screen.buttons.slice(0, 12).join(', ')}`);
  return rows;
}

export function systemPanelRows(system: SystemView): string[] {
  return [
    `CPU: ${system.cpu_count || '--'} CORES`,
    `RAM: ${system.memory_percent ?? '--'}%`,
    `DISK FREE: ${system.disk_free_gb ?? '--'} GB`,
    ...Object.entries(system.services || {}).map(
      ([name, healthy]) => `${name.toUpperCase()}: ${healthy ? 'ONLINE' : 'OFFLINE'}`
    )
  ];
}

export function workflowPanelRows(workflow: WorkflowView): string[] {
  return workflow.steps.map(
    (step) => `${step.status || 'PENDING'} · ${step.title} · ${step.tool || 'AGENT'}`
  );
}

export function tracePanelRows(trace: TraceView[]): string[] {
  return trace.map(
    (item) =>
      `${item.event_type} · ${item.tool || item.model || '--'} · ${Math.round(item.latency_ms)}ms · ` +
      `${item.input_tokens + item.output_tokens} tokens`
  );
}

export function healthPanelRows(health: HealthView): string[] {
  return [
    `OVERALL: ${health.status}`,
    ...health.components.map(
      (component) =>
        `${component.name.toUpperCase()}: ${component.status}` +
        (component.message ? ` · ${component.message}` : '')
    )
  ];
}

export function performancePanelRows(
  trace: TraceView[],
  system: SystemView
): string[] {
  const tokens = trace.reduce(
    (total, item) => total + item.input_tokens + item.output_tokens,
    0
  );
  const cost = trace.reduce((total, item) => total + item.estimated_cost, 0);
  const latency = trace.length
    ? trace.reduce((total, item) => total + item.latency_ms, 0) / trace.length
    : 0;
  return [
    `AVG LATENCY: ${Math.round(latency)} ms`,
    `TRACE TOKENS: ${tokens}`,
    `TRACE COST: $${cost.toFixed(6)}`,
    `CPU: ${system.cpu_count || '--'} CORES`,
    `RAM: ${system.memory_percent ?? '--'}%`,
    `DISK FREE: ${system.disk_free_gb ?? '--'} GB`
  ];
}

function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (bridgeToken) headers.set('X-Jarvis-Token', bridgeToken);
  const response = await fetch(`${BRIDGE_URL}${path}`, { ...init, headers });
  const payload = await response.json() as T & { error?: string };
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

export async function initializeRuntimePanels(root: HTMLElement): Promise<void> {
  try {
    const config = await window.jarvis.getVoiceSessionConfig();
    bridgeToken = config.bridgeToken?.trim() || '';
  } catch {
    bridgeToken = '';
  }
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'hud-panel-toggle';
  toggle.textContent = 'STATUS CENTER';
  root.append(toggle);

  const panel = document.createElement('section');
  panel.className = 'hud-runtime-panels';
  panel.hidden = true;
  panel.innerHTML = `
    <header>
      <div>
        <span>AI STATUS CENTER</span>
        <b id="runtime-panel-title">MEMORY</b>
      </div>
      <button type="button" data-runtime-close>CLOSE</button>
    </header>
    <nav>
      <button type="button" data-runtime-tab="memory">MEMORY</button>
      <button type="button" data-runtime-tab="tasks">TASKS</button>
      <button type="button" data-runtime-tab="context">CONTEXT</button>
      <button type="button" data-runtime-tab="executions">EXECUTION</button>
      <button type="button" data-runtime-tab="workflows">WORKFLOW</button>
      <button type="button" data-runtime-tab="screen">SCREEN</button>
      <button type="button" data-runtime-tab="system">SYSTEM</button>
      <button type="button" data-runtime-tab="performance">PERFORMANCE</button>
      <button type="button" data-runtime-tab="trace">TRACE</button>
      <button type="button" data-runtime-tab="persona">PERSONA</button>
      <button type="button" data-runtime-tab="permissions">PERMISSIONS</button>
      <button type="button" data-runtime-tab="data">DATA</button>
      <button type="button" data-runtime-tab="diagnostics">DIAGNOSTICS</button>
      <button type="button" data-runtime-tab="updates">UPDATES</button>
      <button type="button" data-runtime-tab="media">MEDIA</button>
      <button type="button" data-runtime-tab="settings">SETTINGS</button>
    </nav>
    <div class="runtime-panel-status">
      <span>STATUS <b id="runtime-status-mode">--</b></span>
      <span>TASK <b id="runtime-status-task">--</b></span>
      <span>GOAL <b id="runtime-status-goal">--</b></span>
      <span>MODEL <b id="runtime-status-model">--</b></span>
      <span>MEMORY <b id="runtime-status-memory">--</b></span>
      <span>CONTEXT <b id="runtime-status-context">--</b></span>
      <span>LATENCY <b id="runtime-status-latency">--</b></span>
    </div>
    <div class="runtime-panel-body"></div>
  `;
  root.append(panel);

  const title = panel.querySelector<HTMLElement>('#runtime-panel-title');
  const body = panel.querySelector<HTMLElement>('.runtime-panel-body');
  if (!body || !title) return;

  const show = (name: string) => {
    panel.hidden = false;
    title.textContent = name.toUpperCase();
    panel.querySelectorAll<HTMLButtonElement>('[data-runtime-tab]').forEach((button) => {
      button.dataset.active = String(button.dataset.runtimeTab === name);
    });
    void render(name, body);
  };

  toggle.addEventListener('click', () => {
    if (panel.hidden) show('memory');
    else panel.hidden = true;
  });
  panel.querySelector('[data-runtime-close]')?.addEventListener('click', () => {
    panel.hidden = true;
  });
  panel.querySelectorAll<HTMLButtonElement>('[data-runtime-tab]').forEach((button) => {
    button.addEventListener('click', () => show(button.dataset.runtimeTab || 'memory'));
  });
  window.jarvis.onOpenPanel((panelName) => show(panelName));
  window.addEventListener('jarvis:hud-state', ((event: Event) => {
    const detail = (event as CustomEvent<Record<string, unknown>>).detail || {};
    const task = panel.querySelector('#runtime-status-task');
    const goal = panel.querySelector('#runtime-status-goal');
    const model = panel.querySelector('#runtime-status-model');
    const context = panel.querySelector('#runtime-status-context');
    const mode = panel.querySelector('#runtime-status-mode');
    const memory = panel.querySelector('#runtime-status-memory');
    const latency = panel.querySelector('#runtime-status-latency');
    if (mode) mode.textContent = String(detail.status || '--');
    if (task) task.textContent = String(detail.task || '--');
    if (goal) goal.textContent = String(detail.goal || '--');
    if (model) model.textContent = String(detail.model || '--');
    if (memory) memory.textContent = String(detail.memory || '--');
    if (context) context.textContent = String(detail.context || '--');
    if (latency) latency.textContent = String(detail.latency || '--');
  }) as EventListener);
}

async function render(name: string, body: HTMLElement): Promise<void> {
  body.innerHTML = '<div class="runtime-panel-empty">LOADING</div>';
  try {
    if (name === 'memory') return await renderMemory(body);
    if (name === 'tasks') return await renderTasks(body);
    if (name === 'context') return await renderContext(body);
    if (name === 'executions') return await renderExecutions(body);
    if (name === 'workflows') return await renderWorkflows(body);
    if (name === 'screen') return await renderScreen(body);
    if (name === 'system') return await renderSystem(body);
    if (name === 'performance') return await renderPerformance(body);
    if (name === 'trace') return await renderTrace(body);
    if (name === 'persona') return await renderPersona(body);
    if (name === 'permissions') return await renderPermissions(body);
    if (name === 'data') return await renderData(body);
    if (name === 'diagnostics') return await renderDiagnostics(body);
    if (name === 'updates') return await renderUpdates(body);
    if (name === 'media') return await renderMedia(body);
    if (name === 'settings') return await renderSettings(body);
  } catch (error) {
    body.innerHTML = `<div class="runtime-panel-empty">${escapeHtml(
      error instanceof Error ? error.message : String(error)
    )}</div>`;
  }
}

async function renderMemory(body: HTMLElement): Promise<void> {
  body.innerHTML = `
    <div class="runtime-panel-search">
      <input type="search" placeholder="SEARCH MEMORY" />
      <button type="button" data-memory-search>SEARCH</button>
    </div>
    <div class="runtime-panel-list" data-memory-list></div>
  `;
  const input = body.querySelector<HTMLInputElement>('input');
  const list = body.querySelector<HTMLElement>('[data-memory-list]');
  if (!list) return;
  const load = async () => {
    const query = input?.value.trim() || '';
    const payload = await fetchJson<{ memories: MemoryView[] }>(
      `/api/memory?limit=80${query ? `&query=${encodeURIComponent(query)}` : ''}`
    );
    list.innerHTML = payload.memories.length
      ? payload.memories.map((memory) => `
        <article class="runtime-panel-item">
          <div><span>${escapeHtml(memory.category)}</span><em>${escapeHtml(
            `${Math.round(memory.confidence * 100)}%`
          )}</em></div>
          <p>${escapeHtml(memory.content)}</p>
          <footer>
            <button type="button" data-memory-edit="${memory.id}">EDIT</button>
            <button type="button" data-memory-delete="${memory.id}">DELETE</button>
          </footer>
        </article>`).join('')
      : '<div class="runtime-panel-empty">NO MEMORY</div>';
    window.dispatchEvent(new CustomEvent('jarvis:hud-state', {
      detail: { memory: `${payload.memories.length} ENTRIES` }
    }));
    list.querySelectorAll<HTMLButtonElement>('[data-memory-edit]').forEach((button) => {
      button.addEventListener('click', async () => {
        const current = payload.memories.find(
          (item) => item.id === Number(button.dataset.memoryEdit)
        );
        if (!current) return;
        const content = window.prompt('Memory content', current.content);
        if (!content || content === current.content) return;
        await fetchJson(`/api/memory/${current.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content })
        });
        await load();
      });
    });
    list.querySelectorAll<HTMLButtonElement>('[data-memory-delete]').forEach((button) => {
      button.addEventListener('click', async () => {
        await fetchJson(`/api/memory/${button.dataset.memoryDelete}`, {
          method: 'DELETE'
        });
        await load();
      });
    });
  };
  body.querySelector('[data-memory-search]')?.addEventListener('click', () => void load());
  input?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') void load();
  });
  await load();
}

async function renderTasks(body: HTMLElement): Promise<void> {
  const payload = await fetchJson<{ tasks: TaskView[] }>('/api/tasks?limit=80');
  const latestSession = payload.tasks.find((task) => task.session_id)?.session_id || '';
  window.dispatchEvent(new CustomEvent('jarvis:hud-state', {
    detail: {
      task: payload.tasks[0]?.goal || '--',
      goal: payload.tasks[0]?.goal || '--',
      context: latestSession || '--'
    }
  }));
  body.innerHTML = payload.tasks.length
    ? payload.tasks.map((task) => `
      <article class="runtime-panel-item">
        <div><span>${escapeHtml(task.status)}</span><em>${Math.round(task.progress * 100)}%</em></div>
        <p>${escapeHtml(task.goal)}</p>
        <footer>
          <button type="button" data-task-action="pause" data-task-id="${escapeHtml(task.id)}">PAUSE</button>
          <button type="button" data-task-action="resume" data-task-id="${escapeHtml(task.id)}">RESUME</button>
          <button type="button" data-task-action="cancel" data-task-id="${escapeHtml(task.id)}">CANCEL</button>
        </footer>
      </article>`).join('')
    : '<div class="runtime-panel-empty">NO TASKS</div>';
  body.querySelectorAll<HTMLButtonElement>('[data-task-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      await fetchJson(
        `/api/tasks/${encodeURIComponent(button.dataset.taskId || '')}/${button.dataset.taskAction}`,
        { method: 'POST' }
      );
      await renderTasks(body);
    });
  });
}

async function renderContext(body: HTMLElement): Promise<void> {
  const tasks = await fetchJson<{ tasks: TaskView[] }>('/api/tasks?limit=80');
  const sessionId = tasks.tasks.find((task) => task.session_id)?.session_id;
  if (!sessionId) {
    body.innerHTML = '<div class="runtime-panel-empty">NO ACTIVE SESSION</div>';
    return;
  }
  const payload = await fetchJson<{ context: ContextView }>(
    `/api/context?session_id=${encodeURIComponent(sessionId)}&query=${encodeURIComponent('当前任务 用户偏好')}`
  );
  const rows = contextPanelRows(payload.context);
  body.innerHTML = `
    <div class="runtime-panel-context">
      <span>SESSION ${escapeHtml(sessionId)}</span>
      ${rows.map((row) => `<p>${escapeHtml(row)}</p>`).join('') || '<em>EMPTY CONTEXT</em>'}
    </div>
  `;
  window.dispatchEvent(new CustomEvent('jarvis:hud-state', {
    detail: {
      context: payload.context.summary || sessionId,
      goal: payload.context.goal || '--',
      memory: `${payload.context.memories?.length || 0} IN CONTEXT`
    }
  }));
}

async function renderPersona(body: HTMLElement): Promise<void> {
  const payload = await fetchJson<{ persona: Record<string, unknown> }>('/api/persona');
  body.innerHTML = `<pre class="runtime-panel-json">${escapeHtml(
    JSON.stringify(payload.persona, null, 2)
  )}</pre>`;
}

async function renderExecutions(body: HTMLElement): Promise<void> {
  const payload = await fetchJson<{ executions: ExecutionView[] }>(
    '/api/executions?limit=40'
  );
  body.innerHTML = payload.executions.length
    ? payload.executions.map((execution) => {
      const rows = executionPanelRows(execution);
      return `
        <article class="runtime-panel-item execution-item">
          <div>
            <span>${escapeHtml(execution.status)}</span>
            <em>${escapeHtml(execution.execution_id.slice(0, 8))}</em>
          </div>
          <p>${escapeHtml(execution.goal)}</p>
          <div class="execution-timeline">
            ${rows.map((row) => `<span>${escapeHtml(row)}</span>`).join('')}
          </div>
          <footer>
            <button type="button" data-execution-action="pause" data-execution-id="${escapeHtml(execution.execution_id)}">PAUSE</button>
            <button type="button" data-execution-action="resume" data-execution-id="${escapeHtml(execution.execution_id)}">RESUME</button>
            <button type="button" data-execution-action="cancel" data-execution-id="${escapeHtml(execution.execution_id)}">CANCEL</button>
          </footer>
        </article>`;
    }).join('')
    : '<div class="runtime-panel-empty">NO EXECUTIONS</div>';
  body.querySelectorAll<HTMLButtonElement>('[data-execution-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      const executionId = button.dataset.executionId || '';
      const action = button.dataset.executionAction || '';
      await fetchJson(
        `/api/executions/${encodeURIComponent(executionId)}/${action}`,
        { method: 'POST' }
      );
      await renderExecutions(body);
    });
  });
}

async function renderWorkflows(body: HTMLElement): Promise<void> {
  const payload = await fetchJson<{ workflows: WorkflowView[] }>(
    '/api/workflows?limit=40'
  );
  body.innerHTML = payload.workflows.length
    ? payload.workflows.map((workflow) => `
      <article class="runtime-panel-item">
        <div><span>${escapeHtml(workflow.status)}</span><em>${escapeHtml(workflow.workflow_id.slice(0, 8))}</em></div>
        <p>${escapeHtml(workflow.name)}</p>
        <div class="execution-timeline">
          ${workflowPanelRows(workflow).map((row) => `<span>${escapeHtml(row)}</span>`).join('')}
        </div>
        <footer>
          <button type="button" data-workflow-action="run" data-workflow-id="${escapeHtml(workflow.workflow_id)}">RUN</button>
          <button type="button" data-workflow-action="cancel" data-workflow-id="${escapeHtml(workflow.workflow_id)}">CANCEL</button>
        </footer>
      </article>`).join('')
    : '<div class="runtime-panel-empty">NO WORKFLOWS</div>';
  body.querySelectorAll<HTMLButtonElement>('[data-workflow-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      await fetchJson(
        `/api/workflows/${encodeURIComponent(button.dataset.workflowId || '')}/${button.dataset.workflowAction}`,
        { method: 'POST' }
      );
      await renderWorkflows(body);
    });
  });
}

async function renderScreen(body: HTMLElement): Promise<void> {
  const payload = await fetchJson<{ screen: ScreenView }>('/api/screen');
  body.innerHTML = `<div class="runtime-panel-context">${screenPanelRows(
    payload.screen
  ).map((row) => `<p>${escapeHtml(row)}</p>`).join('')}</div>`;
}

async function renderSystem(body: HTMLElement): Promise<void> {
  const [payload, healthPayload] = await Promise.all([
    fetchJson<{ system: SystemView }>('/api/system/monitor'),
    fetchJson<{ health: HealthView }>('/health/details')
  ]);
  const rows = [
    ...healthPanelRows(healthPayload.health),
    ...systemPanelRows(payload.system)
  ];
  body.innerHTML = `<div class="runtime-panel-context">${rows
    .map((row) => `<p>${escapeHtml(row)}</p>`)
    .join('')}</div>`;
}

async function renderTrace(body: HTMLElement): Promise<void> {
  const payload = await fetchJson<{ trace: TraceView[] }>('/api/trace?limit=200');
  body.innerHTML = payload.trace.length
    ? `<div class="runtime-panel-list">${tracePanelRows(payload.trace)
      .map((row) => `<div class="runtime-panel-item"><p>${escapeHtml(row)}</p></div>`)
      .join('')}</div>`
    : '<div class="runtime-panel-empty">NO TRACE</div>';
}

async function renderPerformance(body: HTMLElement): Promise<void> {
  const [tracePayload, systemPayload] = await Promise.all([
    fetchJson<{ trace: TraceView[] }>('/api/trace?limit=500'),
    fetchJson<{ system: SystemView }>('/api/system/monitor')
  ]);
  body.innerHTML = `<div class="runtime-panel-context">${performancePanelRows(
    tracePayload.trace,
    systemPayload.system
  )
    .map((row) => `<p>${escapeHtml(row)}</p>`)
    .join('')}</div>`;
}

async function renderPermissions(body: HTMLElement): Promise<void> {
  const payload = await fetchJson<{ permissions: PermissionCenterView }>(
    '/api/production/permissions'
  );
  body.innerHTML = `
    <div class="runtime-panel-list">
      ${payload.permissions.categories.map((category) => `
        <article class="runtime-panel-item">
          <div>
            <span>${escapeHtml(category.name)}</span>
            <em>${escapeHtml(category.mode)}</em>
          </div>
          <label class="runtime-panel-field">
            <span>ACCESS MODE</span>
            <select data-permission-category="${escapeHtml(category.key)}">
              ${category.options.map((option) => `
                <option value="${escapeHtml(option)}" ${
                  option === category.mode ? 'selected' : ''
                }>${escapeHtml(option)}</option>`).join('')}
            </select>
          </label>
        </article>`).join('')}
    </div>
  `;
  body.querySelectorAll<HTMLSelectElement>('[data-permission-category]').forEach((select) => {
    select.addEventListener('change', async () => {
      await fetchJson('/api/production/permissions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          category: select.dataset.permissionCategory,
          mode: select.value
        })
      });
      await renderPermissions(body);
    });
  });
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
  if (value < 1_073_741_824) return `${(value / 1_048_576).toFixed(1)} MB`;
  return `${(value / 1_073_741_824).toFixed(2)} GB`;
}

type DataView = {
  items?: Array<Record<string, unknown>>;
  overview?: ProductionOverview;
};

async function renderData(body: HTMLElement): Promise<void> {
  const [overviewPayload, backupsPayload] = await Promise.all([
    fetchJson<{ overview: ProductionOverview }>('/api/production/data'),
    fetchJson<{ items: Array<Record<string, unknown>> }>(
      '/api/production/data?section=backups'
    )
  ]);
  const overview = overviewPayload.overview;
  const backupRows = backupsPayload.items.map((item) =>
    `${String(item.name || '')} · ${String(item.kind || '')} · ${formatBytes(
      Number(item.size_bytes || 0)
    )}`
  );
  body.innerHTML = `
    <div class="runtime-panel-actions">
      <button type="button" data-data-backup>CREATE BACKUP</button>
      <button type="button" data-data-clean>CLEAN SAFE DATA</button>
      <button type="button" data-data-export>EXPORT OVERVIEW</button>
    </div>
    <div class="runtime-panel-context">
      <p>MEMORY: ${overview.memory_count}</p>
      <p>TASKS: ${overview.task_count} · ACTIVE: ${overview.active_task_count}</p>
      <p>CONVERSATIONS: ${overview.conversation_count} · MESSAGES: ${overview.message_count}</p>
      <p>DATABASE: ${formatBytes(overview.database_size_bytes)}</p>
      <p>CACHE: ${formatBytes(overview.cache_size_bytes)}</p>
      <p>LOGS: ${overview.log.files} FILES · ${formatBytes(overview.log.size_bytes)}</p>
      <p>BACKUPS: ${overview.backup_count}</p>
    </div>
    <div class="runtime-panel-list">
      ${backupRows.length
        ? backupsPayload.items.map((item) => `
          <article class="runtime-panel-item">
            <div><span>${escapeHtml(item.name)}</span><em>${escapeHtml(item.kind)}</em></div>
            <p>${escapeHtml(item.created_at || '')} · ${formatBytes(Number(item.size_bytes || 0))}</p>
            <footer>
              <button type="button" data-restore-backup="${escapeHtml(item.path)}">RESTORE</button>
            </footer>
          </article>`).join('')
        : '<div class="runtime-panel-empty">NO BACKUPS</div>'}
    </div>
    <div class="runtime-panel-search">
      <select data-data-section>
        <option value="conversations">CONVERSATIONS</option>
        <option value="tasks">TASKS</option>
        <option value="memory">MEMORY</option>
        <option value="logs">LOGS</option>
      </select>
      <input type="search" placeholder="SEARCH DATA" />
      <button type="button" data-data-search>SEARCH</button>
    </div>
    <div class="runtime-panel-list" data-data-results></div>
  `;
  const results = body.querySelector<HTMLElement>('[data-data-results]');
  const section = body.querySelector<HTMLSelectElement>('[data-data-section]');
  const search = body.querySelector<HTMLInputElement>('input[type="search"]');
  const runSearch = async () => {
    if (!results || !section) return;
    const payload = await fetchJson<DataView>(
      `/api/production/data?section=${encodeURIComponent(section.value)}&query=${encodeURIComponent(search?.value.trim() || '')}`
    );
    results.innerHTML = payload.items?.length
      ? payload.items.map((item) => `
        <article class="runtime-panel-item">
          <pre class="runtime-panel-json">${escapeHtml(
            JSON.stringify(item, null, 2)
          )}</pre>
        </article>`).join('')
      : '<div class="runtime-panel-empty">NO DATA</div>';
  };
  body.querySelector('[data-data-search]')?.addEventListener('click', () => void runSearch());
  search?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') void runSearch();
  });
  body.querySelector('[data-data-backup]')?.addEventListener('click', async () => {
    await fetchJson('/api/production/backups', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'manual', label: 'ui' })
    });
    await renderData(body);
  });
  body.querySelector('[data-data-clean]')?.addEventListener('click', async () => {
    if (!window.confirm('Clean cache, expired memory, and old logs?')) return;
    await fetchJson('/api/production/data?section=cleanup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        confirmed: true,
        cache: true,
        expired_memory: true,
        logs: true
      })
    });
    await renderData(body);
  });
  body.querySelector('[data-data-export]')?.addEventListener('click', async () => {
    const payload = await fetchJson<{ path: string }>('/api/production/data/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section: 'overview' })
    });
    window.alert(`Exported to ${payload.path}`);
  });
  body.querySelectorAll<HTMLButtonElement>('[data-restore-backup]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!window.confirm('Restore this backup on the next restart?')) return;
      await fetchJson('/api/production/backups/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: button.dataset.restoreBackup,
          confirmed: true
        })
      });
      window.alert('暂存恢复已就绪。重启丽萨后生效。');
    });
  });
}

async function renderDiagnostics(body: HTMLElement): Promise<void> {
  const payload = await fetchJson<{ report: DiagnosticReport }>(
    '/api/production/diagnostics'
  );
  const report = payload.report;
  body.innerHTML = `
    <div class="runtime-panel-actions">
      <button type="button" data-diagnostics-run>RUN CHECK</button>
      <button type="button" data-diagnostics-export>EXPORT REPORT</button>
    </div>
    <div class="runtime-panel-context">
      <p>STATUS: ${escapeHtml(report.status)}</p>
      <p>ENVIRONMENT: ${escapeHtml(report.environment)}</p>
      <p>VERSION: ${escapeHtml(report.version)}</p>
      <p>GENERATED: ${escapeHtml(report.generated_at)}</p>
      ${report.findings.length
        ? report.findings.map((finding) => `<p>${escapeHtml(finding)}</p>`).join('')
        : '<p>NO FINDINGS</p>'}
    </div>
    <pre class="runtime-panel-json">${escapeHtml(
      JSON.stringify(report.checks, null, 2)
    )}</pre>
  `;
  body.querySelector('[data-diagnostics-run]')?.addEventListener('click', () => {
    void renderDiagnostics(body);
  });
  body.querySelector('[data-diagnostics-export]')?.addEventListener('click', async () => {
    const exported = await fetchJson<{ export_path: string }>(
      '/api/production/diagnostics?export=1',
      { method: 'POST' }
    );
    window.alert(`Diagnostic report exported to ${exported.export_path}`);
  });
}

async function renderUpdates(body: HTMLElement): Promise<void> {
  const [updatePayload, configPayload] = await Promise.all([
    fetchJson<{ update: UpdateView }>('/api/production/update'),
    fetchJson<{ config: Record<string, any> }>('/api/production/config')
  ]);
  const update = updatePayload.update;
  const manifestUrl = String(configPayload.config.updates?.manifest_url || '');
  body.innerHTML = `
    <div class="runtime-panel-actions">
      <button type="button" data-update-check>CHECK</button>
      <button type="button" data-update-download>DOWNLOAD</button>
      <button type="button" data-update-install>INSTALL</button>
      <button type="button" data-update-rollback>ROLLBACK</button>
    </div>
    <label class="runtime-panel-field">
      <span>MANIFEST URL</span>
      <input type="url" data-update-url value="${escapeHtml(manifestUrl)}" />
    </label>
    <div class="runtime-panel-context">
      <p>STATUS: ${escapeHtml(update.status)}</p>
      <p>CURRENT: v${escapeHtml(update.current_version)}</p>
      <p>AVAILABLE: ${escapeHtml(update.available?.version || '--')}</p>
      <p>SIGNATURE: ${update.signature_verified ? 'VERIFIED' : 'NOT VERIFIED'}</p>
      <p>${escapeHtml(update.last_error || update.available?.notes || '')}</p>
    </div>
  `;
  const action = async (name: string, extra: Record<string, unknown> = {}) => {
    const url = body.querySelector<HTMLInputElement>('[data-update-url]')?.value.trim() || '';
    const payload = await fetchJson<{ update: UpdateView }>('/api/production/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: name, url, ...extra })
    });
    if (name === 'install' || name === 'rollback') {
      window.alert(JSON.stringify(payload.update, null, 2));
    }
    await renderUpdates(body);
  };
  body.querySelector('[data-update-check]')?.addEventListener('click', () => void action('check'));
  body.querySelector('[data-update-download]')?.addEventListener('click', () => void action('download'));
  body.querySelector('[data-update-install]')?.addEventListener('click', () => {
    if (window.confirm('Install the verified update now?')) void action('install', { confirmed: true });
  });
  body.querySelector('[data-update-rollback]')?.addEventListener('click', () => {
    if (window.confirm('Roll back to the backup created before the last update?')) {
      void action('rollback', { confirmed: true });
    }
  });
}

async function renderSettings(body: HTMLElement): Promise<void> {
  const [payload, desktop] = await Promise.all([
    fetchJson<{
      config: Record<string, any>;
      secrets: { secrets: Record<string, { configured: boolean; masked: string }> };
    }>('/api/production/config'),
    window.jarvis.getDesktopSettings()
  ]);
  const config = payload.config;
  const apiKey = payload.secrets.secrets.model_api_key;
  body.innerHTML = `
    <div class="runtime-panel-context">
      <label class="runtime-panel-field">
        <span>LLM MODEL</span>
        <input data-setting-model value="${escapeHtml(config.llm?.model || '')}" />
      </label>
      <label class="runtime-panel-field">
        <span>LLM BASE URL</span>
        <input data-setting-base-url value="${escapeHtml(config.llm?.base_url || '')}" />
      </label>
      <label class="runtime-panel-field">
        <span>API KEY</span>
        <input data-setting-api-key type="password" placeholder="${
          apiKey?.configured ? escapeHtml(apiKey.masked) : 'NOT CONFIGURED'
        }" />
      </label>
      <label class="runtime-panel-field">
        <span>PERFORMANCE MODE</span>
        <select data-setting-performance>
          ${['BALANCED', 'LOW_POWER', 'HIGH_PERFORMANCE'].map((mode) => `
            <option value="${mode}" ${
              mode === config.performance?.mode ? 'selected' : ''
            }>${mode}</option>`).join('')}
        </select>
      </label>
      <label class="runtime-panel-field">
        <span>UPDATE MANIFEST</span>
        <input data-setting-update-url value="${escapeHtml(
          config.updates?.manifest_url || ''
        )}" />
      </label>
      <label class="runtime-panel-check">
        <input data-setting-offline type="checkbox" ${
          config.network?.offline_mode ? 'checked' : ''
        } />
        <span>OFFLINE MODE</span>
      </label>
      <label class="runtime-panel-check">
        <input data-setting-wake type="checkbox" ${
          config.ui?.wake_word_enabled !== false ? 'checked' : ''
        } />
        <span>WAKE WORD</span>
      </label>
      <label class="runtime-panel-check">
        <input data-setting-tray type="checkbox" ${
          desktop.closeToTray ? 'checked' : ''
        } />
        <span>CLOSE TO TRAY</span>
      </label>
      <label class="runtime-panel-field">
        <span>WAKE HOTKEY</span>
        <input data-hotkey="wake" value="${escapeHtml(desktop.hotkeys.wake)}" />
      </label>
      <label class="runtime-panel-field">
        <span>STOP HOTKEY</span>
        <input data-hotkey="stop" value="${escapeHtml(desktop.hotkeys.stop)}" />
      </label>
      <label class="runtime-panel-field">
        <span>PAUSE HOTKEY</span>
        <input data-hotkey="pause" value="${escapeHtml(desktop.hotkeys.pause)}" />
      </label>
      <label class="runtime-panel-field">
        <span>RESUME HOTKEY</span>
        <input data-hotkey="resume" value="${escapeHtml(desktop.hotkeys.resume)}" />
      </label>
      <div class="runtime-panel-actions">
        <button type="button" data-settings-save>SAVE</button>
        <button type="button" data-settings-test>TEST CONNECTION</button>
      </div>
    </div>
  `;
  body.querySelector('[data-settings-save]')?.addEventListener('click', async () => {
    const updates: Record<string, unknown> = {
      llm: {
        model: body.querySelector<HTMLInputElement>('[data-setting-model]')?.value || '',
        base_url: body.querySelector<HTMLInputElement>('[data-setting-base-url]')?.value || ''
      },
      performance: {
        mode: body.querySelector<HTMLSelectElement>('[data-setting-performance]')?.value || 'BALANCED'
      },
      updates: {
        manifest_url: body.querySelector<HTMLInputElement>('[data-setting-update-url]')?.value || ''
      },
      network: {
        offline_mode: Boolean(
          body.querySelector<HTMLInputElement>('[data-setting-offline]')?.checked
        )
      },
      ui: {
        wake_word_enabled: Boolean(
          body.querySelector<HTMLInputElement>('[data-setting-wake]')?.checked
        ),
        close_to_tray: Boolean(
          body.querySelector<HTMLInputElement>('[data-setting-tray]')?.checked
        )
      }
    };
    const apiKeyValue = body.querySelector<HTMLInputElement>('[data-setting-api-key]')?.value.trim();
    if (apiKeyValue) {
      (updates.llm as Record<string, unknown>).api_key = apiKeyValue;
    }
    await fetchJson('/api/production/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates)
    });
    await window.jarvis.saveDesktopSettings({
      closeToTray: Boolean(
        body.querySelector<HTMLInputElement>('[data-setting-tray]')?.checked
      ),
      hotkeys: {
        wake: body.querySelector<HTMLInputElement>('[data-hotkey="wake"]')?.value.trim() || '',
        stop: body.querySelector<HTMLInputElement>('[data-hotkey="stop"]')?.value.trim() || '',
        pause: body.querySelector<HTMLInputElement>('[data-hotkey="pause"]')?.value.trim() || '',
        resume: body.querySelector<HTMLInputElement>('[data-hotkey="resume"]')?.value.trim() || ''
      }
    });
    window.alert('设置已保存。重启丽萨后生效。');
    await renderSettings(body);
  });
  body.querySelector('[data-settings-test]')?.addEventListener('click', async () => {
    const health = await fetchJson<{ health: HealthView }>('/health/details');
    const llm = health.health.components.find((component) => component.name === 'llm');
    window.alert(`LLM: ${llm?.status || 'UNKNOWN'}${llm?.message ? ` · ${llm.message}` : ''}`);
  });
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error('Unable to read image.'));
    reader.onload = () => {
      const value = String(reader.result || '');
      resolve(value.includes(',') ? value.slice(value.indexOf(',') + 1) : value);
    };
    reader.readAsDataURL(file);
  });
}

async function renderMedia(body: HTMLElement): Promise<void> {
  body.innerHTML = `
    <div class="media-dropzone" tabindex="0" data-media-dropzone>
      <b>DROP IMAGE HERE</b>
      <span>or paste with Ctrl+V</span>
      <button type="button" data-media-select>SELECT IMAGE</button>
      <input type="file" accept="image/*" hidden data-media-file />
    </div>
    <div class="runtime-panel-context media-result" data-media-result>
      <p>NO IMAGE</p>
    </div>
  `;
  const dropzone = body.querySelector<HTMLElement>('[data-media-dropzone]');
  const input = body.querySelector<HTMLInputElement>('[data-media-file]');
  const result = body.querySelector<HTMLElement>('[data-media-result]');
  if (!dropzone || !input || !result) return;

  const sendImage = async (file: File) => {
    if (!file.type.startsWith('image/')) {
      result.innerHTML = '<p>请选择图片文件。</p>';
      return;
    }
    result.innerHTML = '<p>正在把图片发送给视觉模型...</p>';
    const imageBase64 = await fileToBase64(file);
    const payload = await fetchJson<{
      results: Array<{ modality: string; text: string; error?: string }>;
    }>('/api/multimodal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        image_base64: imageBase64,
        text: '请用中文详细描述这张图片，并指出其中值得注意的内容。'
      })
    });
    const imageResult = payload.results.find((item) => item.modality === 'IMAGE');
    const preview = URL.createObjectURL(file);
    result.innerHTML = `
      <img class="media-preview" src="${preview}" alt="" />
      <p>${escapeHtml(imageResult?.text || imageResult?.error || '没有返回视觉结果。')}</p>
    `;
  };

  body.querySelector('[data-media-select]')?.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    const file = input.files?.[0];
    if (file) void sendImage(file);
  });
  dropzone.addEventListener('click', () => dropzone.focus());
  dropzone.addEventListener('dragover', (event) => {
    event.preventDefault();
    dropzone.dataset.active = 'true';
  });
  dropzone.addEventListener('dragleave', () => {
    dropzone.dataset.active = 'false';
  });
  dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    dropzone.dataset.active = 'false';
    const file = event.dataTransfer?.files?.[0];
    if (file) void sendImage(file);
  });
  dropzone.addEventListener('paste', (event) => {
    const file = Array.from(event.clipboardData?.files || []).find((item) =>
      item.type.startsWith('image/')
    );
    if (!file) return;
    event.preventDefault();
    void sendImage(file);
  });
}
