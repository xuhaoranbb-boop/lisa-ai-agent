import { initializeRuntimePanels } from './runtime-panels';

type RuntimeEventPayload = {
  type?: string;
  data?: Record<string, unknown>;
  task_id?: string | null;
  request_id?: string | null;
};

type HudState = {
  model: string;
  mode: string;
  status: string;
  task: string;
  step: string;
  tool: string;
  mcp: string;
  mcpTools: string;
  cpu: string;
  ram: string;
  gpu: string;
  network: string;
  asr: string;
  llm: string;
  tts: string;
  total: string;
  level: number;
};

const state: HudState = {
  model: 'unknown',
  mode: 'IDLE',
  status: 'STANDBY',
  task: 'AWAITING COMMAND',
  step: 'STANDBY',
  tool: 'STANDBY',
  mcp: 'STANDBY',
  mcpTools: '0',
  cpu: '--',
  ram: '--',
  gpu: '--',
  network: '--',
  asr: '--',
  llm: '--',
  tts: '--',
  total: '--',
  level: 0
};

let waveBars: HTMLElement[] = [];
let waveFrame: number | null = null;
let bridgeToken = '';

function text(id: string, value: string) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function render() {
  const hud = document.querySelector<HTMLElement>('.hud');
  if (hud) hud.dataset.state = state.mode.toLowerCase();
  text('hud-model', state.model);
  text('hud-mode', state.mode);
  text('hud-status', state.status);
  text('hud-task', state.task);
  text('hud-step', state.step);
  text('hud-tool', state.tool);
  text('hud-mcp', state.mcp);
  text('hud-mcp-tools', state.mcpTools);
  text('hud-cpu', state.cpu);
  text('hud-ram', state.ram);
  text('hud-gpu', state.gpu);
  text('hud-network', state.network);
  text('hud-asr-latency', state.asr);
  text('hud-llm-latency', state.llm);
  text('hud-tts-latency', state.tts);
  text('hud-total-latency', state.total);
  window.dispatchEvent(new CustomEvent('jarvis:hud-state', {
    detail: {
      task: state.task,
      goal: state.task,
      model: state.model,
      status: state.status,
      context: state.status,
      latency: state.total,
      memory: '--'
    }
  }));
}

let subtitlePartial = '';
let subtitleTimer: number | null = null;
const SUBTITLE_MAX_CHARS = 140;

function subtitleRow(speaker: 'user' | 'assistant') {
  const row = document.getElementById(`hud-subtitle-${speaker}`);
  const what = document.getElementById(`hud-subtitle-${speaker}-text`);
  if (!row || !what) return null;
  return { row, what };
}

function showVoiceTranscript(text: string, final: boolean, speaker: 'user' | 'assistant') {
  const root = document.getElementById('hud-subtitle');
  const target = subtitleRow(speaker);
  if (!root || !target) return;
  const trimmed = text.trim();
  if (!trimmed) return;
  let display = trimmed;
  if (speaker === 'user') {
    if (!final) {
      if (subtitlePartial === '') {
        const previous = subtitleRow('assistant');
        if (previous) {
          previous.what.textContent = '';
          previous.row.hidden = true;
        }
      }
      subtitlePartial = trimmed.startsWith(subtitlePartial) ? trimmed : subtitlePartial + trimmed;
      display = subtitlePartial;
    } else {
      subtitlePartial = '';
    }
  }
  if (display.length > SUBTITLE_MAX_CHARS) display = display.slice(0, SUBTITLE_MAX_CHARS) + '…';
  target.what.textContent = display;
  target.row.hidden = false;
  root.hidden = false;
  if (subtitleTimer !== null) window.clearTimeout(subtitleTimer);
  subtitleTimer = window.setTimeout(() => {
    root.hidden = true;
    subtitleTimer = null;
  }, final ? 5_000 : 2_500);
}

function createHud() {
  if (document.querySelector('.hud')) return;
  const hud = document.createElement('div');
  hud.className = 'hud';
  hud.setAttribute('aria-hidden', 'true');
  hud.innerHTML = `
    <header class="hud-top">
      <div class="hud-brand"><span>GCAI</span><strong>JARVIS</strong></div>
      <div class="hud-top-metrics">
        <div><span>MODEL</span><b id="hud-model">unknown</b></div>
        <div><span>MODE</span><b id="hud-mode">IDLE</b></div>
        <div><span>STATUS</span><b id="hud-status">STANDBY</b></div>
      </div>
    </header>
    <div class="hud-core" aria-hidden="true">
      <canvas class="hud-particle-cloud-canvas" id="hud-particle-cloud" width="700" height="700"></canvas>
    </div>
    <aside class="hud-left">
      <div class="hud-title">SYSTEM</div>
      <div class="hud-row"><span>CPU</span><b id="hud-cpu">--</b></div>
      <div class="hud-row"><span>RAM</span><b id="hud-ram">--</b></div>
      <div class="hud-meter"><i id="hud-ram-meter"></i></div>
      <div class="hud-row"><span>GPU</span><b id="hud-gpu">--</b></div>
      <div class="hud-row"><span>NETWORK</span><b id="hud-network">--</b></div>
      <div class="hud-divider"></div>
      <div class="hud-title">VOICE</div>
      <div class="hud-row"><span>MIC</span><b>READY</b></div>
      <div class="hud-row"><span>VAD</span><b>ACTIVE</b></div>
      <div class="hud-row"><span>ASR</span><b>WHISPER</b></div>
      <div class="hud-row"><span>TTS</span><b>PIPER</b></div>
    </aside>
    <aside class="hud-right">
      <div class="hud-title">AGENT</div>
      <div class="hud-row"><span>TASK</span><b id="hud-task">AWAITING COMMAND</b></div>
      <div class="hud-row"><span>STEP</span><b id="hud-step">STANDBY</b></div>
      <div class="hud-row"><span>TOOL</span><b id="hud-tool">STANDBY</b></div>
      <div class="hud-divider"></div>
      <div class="hud-title">MCP</div>
      <div class="hud-row"><span>STATUS</span><b id="hud-mcp">STANDBY</b></div>
      <div class="hud-row"><span>TOOLS</span><b id="hud-mcp-tools">0</b></div>
      <div class="hud-divider"></div>
      <div class="hud-title">PLAN</div>
      <div id="hud-plan" class="hud-plan">AWAITING TASK</div>
    </aside>
    <footer class="hud-bottom">
      <div id="hud-wave" class="hud-wave"></div>
      <div class="hud-bottom-meta">
        <span>MIC</span><b>LIVE</b>
        <span>ASR</span><b id="hud-asr-latency">--</b>
        <span>LLM</span><b id="hud-llm-latency">--</b>
        <span>TTS</span><b id="hud-tts-latency">--</b>
        <span>TOTAL</span><b id="hud-total-latency">--</b>
        <span>MODEL</span><b id="hud-bottom-model">unknown</b>
      </div>
    </footer>
    <section id="hud-permission" class="hud-permission" hidden>
      <div class="hud-permission-kicker">PERMISSION REQUIRED</div>
      <div id="hud-permission-action" class="hud-permission-action">ACTION</div>
      <div id="hud-permission-resource" class="hud-permission-resource">RESOURCE</div>
      <div class="hud-permission-actions">
        <button type="button" data-permission-decision="allow_once">允许一次</button>
        <button type="button" data-permission-decision="allow_task">允许本次任务</button>
        <button type="button" data-permission-decision="deny">拒绝</button>
      </div>
    </section>
    <div id="hud-notification" class="hud-notification" hidden>
      <span id="hud-notification-label">JARVIS</span>
      <b id="hud-notification-message"></b>
    </div>
    <div id="hud-subtitle" class="hud-subtitle" hidden>
      <div id="hud-subtitle-user" class="row user" hidden>
        <span class="who">你</span><span class="what" id="hud-subtitle-user-text"></span>
      </div>
      <div id="hud-subtitle-assistant" class="row assistant" hidden>
        <span class="who">丽萨</span><span class="what" id="hud-subtitle-assistant-text"></span>
      </div>
    </div>
  `;
  document.body.append(hud);
  const wave = document.getElementById('hud-wave');
  if (wave) {
    waveBars = Array.from({ length: 48 }, (_, index) => {
      const bar = document.createElement('i');
      bar.style.setProperty('--bar', String(index));
      wave.append(bar);
      return bar;
    });
  }
  createParticleCloud();
  const permission = document.getElementById('hud-permission');
  permission?.querySelectorAll<HTMLButtonElement>('[data-permission-decision]').forEach((button) => {
    button.addEventListener('click', () => {
      const requestId = permission.dataset.requestId || '';
      const decision = button.dataset.permissionDecision || 'deny';
      void resolvePermission(requestId, decision);
    });
  });
}

function createParticleCloud() {
  const canvas = document.getElementById('hud-particle-cloud') as HTMLCanvasElement | null;
  const context = canvas?.getContext('2d');
  if (!canvas || !context) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const logicalSize = 700;
  canvas.width = logicalSize * dpr;
  canvas.height = logicalSize * dpr;
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  const palette = ['#42e8ff', '#75f7ff', '#7c6bff', '#ba5cff', '#ff4f78'];
  const particles: Array<{
    x: number;
    y: number;
    z: number;
    size: number;
    color: string;
    phase: number;
  }> = [];
  const golden = Math.PI * (3 - Math.sqrt(5));
  const sphereCount = 2_200;
  for (let index = 0; index < sphereCount; index += 1) {
    const y = 1 - (index / (sphereCount - 1)) * 2;
    const radius = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = golden * index;
    const band = (y + 1) / 2;
    const colorIndex = Math.min(
      palette.length - 1,
      Math.max(0, Math.floor(band * palette.length + (index % 3) * 0.08))
    );
    particles.push({
      x: Math.cos(theta) * radius * 184,
      y: y * 184,
      z: Math.sin(theta) * radius * 184,
      size: 0.85 + ((index * 17) % 21) / 18,
      color: palette[colorIndex],
      phase: index * 0.37
    });
  }
  for (let ring = 0; ring < 3; ring += 1) {
    const ringCount = ring === 0 ? 360 : 240;
    const tilt = (ring - 1) * 0.48;
    for (let index = 0; index < ringCount; index += 1) {
      const angle = (index / ringCount) * Math.PI * 2;
      const radius = ring === 0 ? 205 : 178 + ring * 9;
      const x = Math.cos(angle) * radius;
      const z = Math.sin(angle) * radius;
      particles.push({
        x,
        y: z * Math.sin(tilt) + (ring - 1) * 8,
        z: z * Math.cos(tilt),
        size: ring === 0 ? 1.1 : 0.75,
        color: ring === 0 ? '#7df7ff' : palette[(ring + 1) % palette.length],
        phase: index * 0.21 + ring
      });
    }
  }
  let lastFrameAt = 0;

  const renderCloud = (timestamp: number) => {
    if (timestamp - lastFrameAt >= 1000 / 30) {
      const interactive = state.mode === 'listening' || state.mode === 'speaking';
      const energy = interactive ? 1 : state.mode === 'thinking' ? 0.72 : 0.32;
      const seconds = timestamp / 1000;
      const center = logicalSize / 2;
      const yaw = seconds * (0.22 + energy * 0.34);
      const pitch = Math.sin(seconds * 0.43) * (0.08 + energy * 0.08);
      const yawCos = Math.cos(yaw);
      const yawSin = Math.sin(yaw);
      const pitchCos = Math.cos(pitch);
      const pitchSin = Math.sin(pitch);
      const breath = 1 + Math.sin(seconds * 1.7) * (0.018 + energy * 0.018);
      context.clearRect(0, 0, logicalSize, logicalSize);
      context.globalCompositeOperation = 'lighter';
      particles.forEach((particle, index) => {
        const swirl = Math.sin(seconds * (0.7 + energy * 1.5) + particle.phase);
        const radial = Math.hypot(particle.x, particle.y, particle.z) || 1;
        const radiusScale = 1 + swirl * (0.012 + energy * 0.028);
        const x = particle.x * radiusScale;
        const y = particle.y * radiusScale;
        const z = particle.z * radiusScale;
        const yawX = x * yawCos + z * yawSin;
        const yawZ = -x * yawSin + z * yawCos;
        const rotatedY = y * pitchCos - yawZ * pitchSin;
        const rotatedZ = y * pitchSin + yawZ * pitchCos;
        const perspective = 820 / Math.max(320, 820 - rotatedZ * 1.25);
        const px = center + yawX * perspective * breath;
        const py = center + rotatedY * perspective * breath;
        const energyWave = Math.max(
          0,
          Math.sin(seconds * 3.2 - radial * 0.034 + swirl * 0.25)
        );
        const particleSize = particle.size
          * (0.72 + perspective * 0.3)
          * (1 + energyWave * (interactive ? 0.34 : 0.12));
        const alpha = Math.min(
          1,
          (0.16 + perspective * 0.2)
            * (interactive ? 1 : 0.72)
            * (0.86 + energyWave * 0.32)
        );
        if (px < -20 || px > logicalSize + 20 || py < -20 || py > logicalSize + 20) {
          return;
        }
        context.globalAlpha = alpha;
        context.fillStyle = particle.color;
        context.beginPath();
        context.arc(px, py, particleSize, 0, Math.PI * 2);
        context.fill();
        if (index % 29 === 0) {
          context.globalAlpha = alpha * 0.18;
          context.fillStyle = '#ffffff';
          context.beginPath();
          context.arc(px, py, particleSize * 2.1, 0, Math.PI * 2);
          context.fill();
        }
      });
      context.globalAlpha = 1;
      context.globalCompositeOperation = 'source-over';
      lastFrameAt = timestamp;
    }
    window.requestAnimationFrame(renderCloud);
  };
  window.requestAnimationFrame(renderCloud);
}

function showPermissionPrompt(data: Record<string, unknown>) {
  const permission = document.getElementById('hud-permission');
  if (!permission) return;
  console.log(`[Ashley] HUD permission request ${String(data.action || 'unknown')}.`);
  permission.dataset.requestId = String(data.id || '');
  const action = document.getElementById('hud-permission-action');
  const resource = document.getElementById('hud-permission-resource');
  if (action) action.textContent = String(data.action || 'UNKNOWN ACTION');
  if (resource) {
    resource.textContent = String(
      data.confirmation_message || data.resource || 'NO TARGET'
    );
  }
  permission.hidden = false;
}

function hidePermissionPrompt() {
  const permission = document.getElementById('hud-permission');
  if (!permission) return;
  permission.hidden = true;
  permission.dataset.requestId = '';
}

let notificationTimer: number | null = null;

function showNotification(message: string, label = '丽萨') {
  const notification = document.getElementById('hud-notification');
  const messageElement = document.getElementById('hud-notification-message');
  const labelElement = document.getElementById('hud-notification-label');
  if (!notification || !messageElement || !labelElement) return;
  labelElement.textContent = label;
  messageElement.textContent = message;
  notification.hidden = false;
  if (notificationTimer !== null) window.clearTimeout(notificationTimer);
  notificationTimer = window.setTimeout(() => {
    notification.hidden = true;
    notificationTimer = null;
  }, 6_000);
}

async function resolvePermission(requestId: string, decision: string) {
  if (!requestId) return;
  hidePermissionPrompt();
  try {
    await fetch(`http://127.0.0.1:8765/api/permissions/${encodeURIComponent(requestId)}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(bridgeToken ? { 'X-Jarvis-Token': bridgeToken } : {})
      },
      body: JSON.stringify({ decision })
    });
  } catch {
    state.status = 'PERMISSION ERROR';
    render();
  }
}

function updateWave(level: number) {
  state.level = Math.max(0, Math.min(1, level));
  const hudRoot = document.querySelector<HTMLElement>('.hud');
  if (hudRoot) hudRoot.style.setProperty('--hud-level', state.level.toFixed(3));
  const amplitude = 0.08 + state.level * 0.92;
  const now = performance.now() / 180;
  waveBars.forEach((bar, index) => {
    const wave = 0.25 + Math.abs(Math.sin(now + index * 0.47)) * amplitude;
    bar.style.transform = `scaleY(${wave.toFixed(3)})`;
  });
  if (waveFrame === null) {
    waveFrame = requestAnimationFrame(() => {
      waveFrame = null;
      if (state.level > 0.01) updateWave(state.level);
    });
  }
}

function applyPlan(plan: unknown) {
  const container = document.getElementById('hud-plan');
  if (!container) return;
  if (!Array.isArray(plan) || plan.length === 0) {
    container.textContent = 'AWAITING TASK';
    return;
  }
  container.textContent = '';
  plan.slice(0, 8).forEach((item, index) => {
    const row = document.createElement('div');
    row.className = 'hud-plan-row';
    const step = item && typeof item === 'object' ? item as Record<string, unknown> : {};
    row.innerHTML = `<span>${String(index + 1).padStart(2, '0')}</span><b>${String(step.title || 'Step')}</b><em>${String(step.status || '')}</em>`;
    container.append(row);
  });
}

function applySystemInfo(value: unknown) {
  if (!value || typeof value !== 'object') return;
  const result = value as Record<string, unknown>;
  if (typeof result.cpu_count === 'number') state.cpu = `${result.cpu_count} CORES`;
  if (typeof result.memory_percent === 'number') {
    const memory = Math.round(result.memory_percent);
    state.ram = `${memory}%`;
    const meter = document.getElementById('hud-ram-meter');
    if (meter) meter.style.width = `${Math.min(100, Math.max(0, memory))}%`;
  }
  if (typeof result.gpu === 'string') state.gpu = formatGpuName(result.gpu);
  render();
}

function formatGpuName(value: string): string {
  return value
    .replace(/^NVIDIA\s+/i, '')
    .replace(/^AMD\s+/i, '')
    .replace(/^Intel\(R\)\s+/i, 'Intel ')
    .replace(/\s+Laptop GPU$/i, '')
    .replace(/\s+Graphics$/i, '')
    .trim();
}

async function refreshBridgeStatus() {
  try {
    const [healthResponse, systemResponse] = await Promise.all([
      fetch('http://127.0.0.1:8765/health'),
      fetch('http://127.0.0.1:8765/api/system', {
        headers: bridgeToken ? { 'X-Jarvis-Token': bridgeToken } : {}
      })
    ]);
    if (healthResponse.ok) {
      const health = await healthResponse.json() as { model?: string };
      if (health.model) state.model = health.model;
      state.network = 'ONLINE';
    } else {
      state.network = 'OFFLINE';
    }
    if (systemResponse.ok) {
      const system = await systemResponse.json() as { system?: unknown };
      applySystemInfo(system.system);
    }
  } catch {
    state.network = 'OFFLINE';
  }
  const bottomModel = document.getElementById('hud-bottom-model');
  if (bottomModel) bottomModel.textContent = state.model;
  render();
}

function scheduleBridgeStatus() {
  void refreshBridgeStatus().finally(() => {
    window.setTimeout(scheduleBridgeStatus, 5_000);
  });
}

function handleRuntimeEvent(payload: unknown) {
  if (!payload || typeof payload !== 'object') return;
  const event = payload as RuntimeEventPayload;
  const data = event.data || {};
  const type = event.type || '';
  if (typeof data.model === 'string') state.model = data.model;
  if (typeof data.state === 'string') state.mode = data.state.toUpperCase();
  const milliseconds = (value: unknown) =>
    typeof value === 'number' && Number.isFinite(value)
      ? `${Math.round(value)} ms`
      : '--';

  switch (type) {
    case 'STATE_CHANGED':
      state.status = String(data.status || state.mode);
      break;
    case 'LLM_START':
      state.mode = 'THINKING';
      state.status = 'REASONING';
      break;
    case 'LLM_FIRST_TOKEN':
      state.llm = milliseconds(data.ttft_ms ?? data.elapsed_ms);
      break;
    case 'MIC_START':
      state.status = 'MIC LIVE';
      break;
    case 'VAD_START':
      state.mode = 'LISTENING';
      state.status = 'VOICE DETECTED';
      break;
    case 'ASR_START':
      state.mode = 'LISTENING';
      state.status = 'TRANSCRIBING';
      break;
    case 'ASR_FIRST':
      state.asr = milliseconds(data.elapsed_ms);
      break;
    case 'ASR_FINAL':
      state.asr = milliseconds(data.elapsed_ms);
      state.status = 'TRANSCRIPT READY';
      break;
    case 'TTS_START':
      state.status = 'SYNTHESIZING';
      break;
    case 'TTS_FIRST_AUDIO':
      state.tts = milliseconds(data.ttfa_ms ?? data.elapsed_ms);
      break;
    case 'PLAYBACK_START':
      state.mode = 'SPEAKING';
      state.status = 'SPEAKING';
      break;
    case 'VOICE_LATENCY':
      state.asr = milliseconds(data.asr_ms);
      state.llm = milliseconds(data.llm_ttft_ms);
      state.tts = milliseconds(data.tts_ttfa_ms);
      state.total = milliseconds(data.total_ms);
      break;
    case 'INTERRUPT':
      state.mode = 'INTERRUPTED';
      state.status = 'INTERRUPTED';
      break;
    case 'TASK_START':
      state.task = String(data.goal || data.message || 'ACTIVE TASK');
      state.status = 'PLANNING';
      break;
    case 'EXECUTION_STARTED':
      state.mode = 'EXECUTING';
      state.status = 'QUEUED';
      state.task = String(data.goal || state.task);
      break;
    case 'EXECUTION_STEP':
      {
        const step = data.step && typeof data.step === 'object'
          ? data.step as Record<string, unknown>
          : {};
        state.mode = 'TOOL_EXECUTING';
        state.status = String(step.status || 'RUNNING');
        state.step = String(step.title || 'STEP');
        state.tool = String(step.tool || '--');
      }
      break;
    case 'EXECUTION_OBSERVATION':
      state.status = 'OBSERVING';
      break;
    case 'EXECUTION_VERIFIED':
      state.status = 'VERIFYING';
      break;
    case 'EXECUTION_RECOVERED':
      state.status = `RECOVERING: ${String(data.strategy || 'REPLAN')}`;
      break;
    case 'EXECUTION_COMPLETE':
      state.mode = 'COMPLETED';
      state.status = 'COMPLETE';
      state.step = 'VERIFIED';
      break;
    case 'EXECUTION_FAILED':
      state.mode = 'ERROR';
      state.status = 'FAILED';
      break;
    case 'EXECUTION_CANCELLED':
      state.mode = 'IDLE';
      state.status = 'CANCELLED';
      break;
    case 'PLAN_CREATED':
      applyPlan(data.plan);
      state.status = 'EXECUTING';
      break;
    case 'TOOL_REQUEST':
    case 'TOOL_START':
      state.tool = String(data.tool || '--');
      state.mode = 'TOOL_EXECUTING';
      state.status = 'EXECUTING';
      break;
    case 'TOOL_RESULT':
      state.status = 'OBSERVING';
      if (String(data.tool || '') === 'get_system_info') applySystemInfo(data.result);
      break;
    case 'TOOL_ERROR':
      state.status = 'ERROR';
      break;
    case 'MCP_CONNECTED':
      state.mcp = 'CONNECTED';
      state.mcpTools = String(Array.isArray(data.tools) ? data.tools.length : 0);
      break;
    case 'MCP_ERROR':
      state.mcp = 'ERROR';
      break;
    case 'PERMISSION_REQUEST':
      state.status = 'AWAITING CONFIRMATION';
      showPermissionPrompt(data);
      break;
    case 'PERMISSION_RESOLVED':
      hidePermissionPrompt();
      state.status = String(data.decision || 'RESOLVED').toUpperCase();
      break;
    case 'REMINDER_DUE':
      showNotification(String(data.message || 'Reminder due.'), 'REMINDER');
      break;
    case 'PROACTIVE_NOTIFICATION':
      showNotification(
        String(data.message || '丽萨 有可用更新。'),
        `丽萨 · P${String(data.priority || '')}`
      );
      break;
    case 'SYSTEM_ALERT': {
      const alert = data.alert && typeof data.alert === 'object'
        ? data.alert as Record<string, unknown>
        : {};
      showNotification(
        String(alert.message || 'System warning.'),
        `SYSTEM · ${String(alert.severity || 'WARNING').toUpperCase()}`
      );
      break;
    }
    case 'TOKEN_BUDGET': {
      const usage = data.usage && typeof data.usage === 'object'
        ? data.usage as Record<string, unknown>
        : {};
      state.status = `TOKENS ${String(usage.total_tokens || 0)}`;
      break;
    }
    case 'BACKGROUND_TASK_COMPLETED':
      showNotification(String(data.name || 'Background task completed.'), 'TASK COMPLETE');
      break;
    case 'BACKGROUND_TASK_FAILED':
      showNotification(String(data.error || 'Background task failed.'), 'TASK FAILED');
      break;
    case 'TASK_COMPLETE':
      state.mode = 'COMPLETED';
      state.status = 'COMPLETE';
      state.step = 'DONE';
      break;
    case 'TASK_FAILED':
      state.mode = 'ERROR';
      state.status = 'FAILED';
      break;
    case 'TASK_CANCELLED':
      state.mode = 'IDLE';
      state.status = 'CANCELLED';
      state.step = 'CANCELLED';
      break;
  }
  const bottomModel = document.getElementById('hud-bottom-model');
  if (bottomModel) bottomModel.textContent = state.model;
  render();
}

export function initializeHud() {
  createHud();
  render();
  const hud = document.querySelector<HTMLElement>('.hud') || document.body;
  void initializeRuntimePanels(hud);
  window.jarvis.onState((value) => {
    state.mode = value.toUpperCase();
    state.status = value === 'thinking' ? 'REASONING' : value.toUpperCase();
    render();
  });
  window.jarvis.onLevel(updateWave);
  window.jarvis.onRuntimeEvent(handleRuntimeEvent);
  window.jarvis.onVoiceTranscript((payload) => showVoiceTranscript(payload.text, payload.final, payload.speaker));
  updateWave(0);
  void window.jarvis.getVoiceSessionConfig().then((config) => {
    bridgeToken = config.bridgeToken?.trim() || '';
    scheduleBridgeStatus();
  }).catch(() => scheduleBridgeStatus());
}
