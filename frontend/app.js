// EasyAgent 薄前端：任务下发 / SSE 事件流 / 画布 / steer / 产物浏览
// 不跑 agent 逻辑、不持有状态机；只做渲染与 API 调用。
import { graphEditor } from './assets/graph-editor.js';

const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
  );
const trunc = (s, n = 2000) => {
  s = typeof s === 'string' ? s : JSON.stringify(s, null, 2);
  return s.length > n ? s.slice(0, n) + '\n…（截断）' : s;
};
const ts = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });

function flash(text) {
  const el = $('flashMsg');
  el.textContent = text;
  el.style.display = 'block';
  clearTimeout(flash._t);
  flash._t = setTimeout(() => (el.style.display = 'none'), 5000);
}

// ---------------- state ----------------
let runId = null;
let missionId = null;
let lastSeq = 0;
let eventSource = null;
let pollTimer = null;
let terminal = false;
let eventCount = 0;
let budget = { max_steps: 50, max_cost_usd: null, max_wall_clock_s: 1800 };

// ---------------- mission submit ----------------
$('missionForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const goal = $('goal').value.trim();
  if (!goal) return flash('请填写目标 goal');
  const b = {
    max_steps: parseInt($('maxSteps').value, 10) || 50,
    max_wall_clock_s: parseInt($('maxWall').value, 10) || 1800,
  };
  const cost = parseFloat($('maxCost').value);
  if (!Number.isNaN(cost)) b.max_cost_usd = cost;
  budget = { ...b };
  $('bMaxSteps').textContent = b.max_steps;
  $('bMaxCost').textContent = b.max_cost_usd ?? '不限';
  $('bMaxWall').textContent = b.max_wall_clock_s;

  let res;
  try {
    // SPEC §8: POST /missions {goal, budget?} → {mission_id, run_id}
    res = await fetch('/missions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal, budget: b }),
    });
  } catch (err) {
    return flash('提交失败：' + err.message);
  }
  if (!res.ok) return flash('提交失败：HTTP ' + res.status + ' ' + (await res.text()).slice(0, 200));
  const data = await res.json();
  missionId = data.mission_id;
  runId = data.run_id;
  if (!runId) return flash('后端返回缺少 run_id');
  openRun(runId, missionId);
});

// ---------------- run view ----------------
function openRun(id, mid) {
  closeStream();
  terminal = false;
  lastSeq = 0;
  eventCount = 0;
  $('runView').style.display = 'block';
  $('canvasCard').style.display = 'block';
  $('runGrid').style.display = 'grid';
  $('runMeta').textContent = `run ${id}` + (mid ? ` · mission ${mid}` : '');
  $('eventLog').innerHTML = '';
  $('planList').innerHTML = '<li class="hint">等待 plan 事件…</li>';
  $('artifactList').innerHTML = '<span class="hint">等待 artifact 事件…</span>';
  $('bEvents').textContent = '0';
  setStatus('pending');
  initCanvas();
  connectSSE();
  pollRunState();
  pollTimer = setInterval(pollRunState, 2500);
  $('runView').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

const TERMINAL = ['done', 'cancelled', 'failed'];
function setStatus(s) {
  const badge = $('statusBadge');
  const labels = {
    pending: '排队中', running: '运行中', paused: '已暂停',
    awaiting_confirm: '等待人工确认', cancelled: '已取消', done: '完成', failed: '失败',
  };
  badge.textContent = labels[s] || s;
  badge.className = 'badge st-' + s;
  if (TERMINAL.includes(s)) {
    terminal = true;
    closeStream();
  }
}

// SPEC §8: GET /runs/{id} → RunState
async function pollRunState() {
  if (!runId || terminal) return;
  try {
    const res = await fetch(`/runs/${encodeURIComponent(runId)}`);
    if (!res.ok) return;
    const st = await res.json();
    if (st.status) setStatus(st.status);
    if (typeof st.step === 'number') $('bSteps').textContent = st.step;
  } catch {
    /* 后端未就绪时静默 */
  }
}

// ---------------- SSE ----------------
// SPEC §8: GET /runs/{id}/events?after_seq=N → text/event-stream
function connectSSE() {
  closeStream();
  const url = `/runs/${encodeURIComponent(runId)}/events` + (lastSeq ? `?after_seq=${lastSeq}` : '');
  const es = new EventSource(url);
  eventSource = es;
  const TYPES = ['plan', 'thought', 'tool_call', 'tool_result', 'artifact', 'checkpoint', 'status', 'steer', 'done', 'error'];
  es.onmessage = (e) => handleRaw('message', e.data);
  for (const t of TYPES) es.addEventListener(t, (e) => handleRaw(t, e.data));
  es.onerror = () => {
    if (terminal) return;
    logLine('error', 'SSE 连接中断，2s 后按 after_seq=' + lastSeq + ' 重连…');
    es.close();
    if (eventSource === es) eventSource = null;
    setTimeout(() => {
      if (!terminal && !eventSource) connectSSE();
    }, 2000);
  };
}

// 兼容两种服务端写法：
//  (a) 无名事件，data 为 RunEvent 信封 {seq, type, payload, ts}
//  (b) 具名事件，data 为 payload 本体
function handleRaw(eventName, data) {
  let type = eventName === 'message' ? null : eventName;
  let payload = data;
  let seq = null;
  try {
    const obj = JSON.parse(data);
    if (obj && typeof obj === 'object' && 'type' in obj && 'payload' in obj) {
      type = type || obj.type;
      payload = obj.payload;
      if (typeof obj.seq === 'number') seq = obj.seq;
    } else {
      payload = obj;
    }
  } catch {
    /* 纯文本 payload */
  }
  if (!type) type = 'message';
  if (seq !== null && seq > lastSeq) lastSeq = seq;
  eventCount += 1;
  $('bEvents').textContent = eventCount;
  renderEvent(type, payload);
}

function renderEvent(type, payload) {
  switch (type) {
    case 'plan':
      renderPlan(payload);
      logLine('plan', '收到计划（' + planItems(payload).length + ' 步）');
      break;
    case 'thought':
      logLine('thought', trunc(payload?.text ?? payload));
      break;
    case 'tool_call':
      onToolCall(payload);
      break;
    case 'tool_result':
      onToolResult(payload);
      break;
    case 'artifact':
      renderArtifact(payload);
      logLine('artifact', '产物：' + esc(payload?.name ?? '(未命名)'));
      break;
    case 'status':
      if (payload?.status) setStatus(payload.status);
      if (typeof payload?.step === 'number') $('bSteps').textContent = payload.step;
      logLine('status', trunc(payload));
      break;
    case 'checkpoint':
      logLine('status', 'checkpoint @ step ' + (payload?.step ?? '?'));
      break;
    case 'steer':
      logLine('status', 'steer 已接收：' + esc(payload?.action ?? JSON.stringify(payload)));
      break;
    case 'done':
      logLine('status', 'run 结束：' + trunc(payload));
      setStatus(payload?.status || 'done');
      break;
    case 'error':
      logLine('error', trunc(payload));
      break;
    default:
      logLine('message', trunc(payload));
  }
}

function logLine(tag, html) {
  const log = $('eventLog');
  const div = document.createElement('div');
  div.className = 'ev';
  div.innerHTML = `<span class="ts">${ts()}</span><span class="tag ${esc(tag)}">${esc(tag)}</span><div>${html}</div>`;
  // thought/tool 结果内容可能是纯文本，转义后保留换行
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

// ---------------- plan ----------------
function planItems(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.plan)) return payload.plan;
  if (Array.isArray(payload?.steps)) return payload.steps;
  return [];
}
function renderPlan(payload) {
  const items = planItems(payload);
  $('planList').innerHTML = items.length
    ? items.map((s) => `<li>${esc(typeof s === 'string' ? s : s.text || s.name || JSON.stringify(s))}</li>`).join('')
    : '<li class="hint">计划为空</li>';
}

// ---------------- tool_call / tool_result ----------------
let callSeq = 0;
const callNode = new Map(); // call_id → node id

function toolNameOf(p) {
  return p?.name || p?.tool || p?.target || '(未知工具)';
}
function onToolCall(payload) {
  callSeq += 1;
  const name = toolNameOf(payload);
  const args = payload?.args ?? payload?.arguments ?? {};
  const callId = payload?.call_id ?? payload?.id ?? `call-${callSeq}`;
  const nodeId = `step-${callSeq}`;
  callNode.set(String(callId), nodeId);
  const prev = canvasDoc.steps.length ? canvasDoc.steps[canvasDoc.steps.length - 1].id : null;
  canvasDoc.steps.push({
    id: nodeId,
    kind: 'tool',
    target: name,
    input: args && typeof args === 'object' ? args : { value: args },
    depends_on: prev ? [prev] : [],
  });
  canvasDoc.metadata.step_labels[nodeId] = `⓿${callSeq} ${name}`;
  refreshCanvas();
  logLine('tool_call', `<b>${esc(name)}</b><pre>${esc(trunc(args, 1200))}</pre>`);
}

function onToolResult(payload) {
  const callId = payload?.call_id ?? payload?.id;
  const nodeId = callId ? callNode.get(String(callId)) : [...callNode.values()].pop();
  const ok = payload?.ok ?? payload?.success;
  const mark = ok === false ? '✗' : '✓';
  if (nodeId && canvasDoc.metadata.step_labels[nodeId]) {
    canvasDoc.metadata.step_labels[nodeId] += ` ${mark}`;
    refreshCanvas();
  }
  const summary = payload?.summary ?? payload?.result ?? payload?.output ?? payload;
  logLine(
    'tool_result',
    `<b>${esc(ok === false ? '失败' : '成功')}</b><pre>${esc(trunc(summary, 1500))}</pre>`
  );
}

// ---------------- artifacts ----------------
function renderArtifact(payload) {
  const list = $('artifactList');
  if (list.querySelector('.hint')) list.innerHTML = '';
  const name = payload?.name || payload?.path || 'artifact';
  const content = payload?.content ?? payload?.text ?? '';
  const mediaType = payload?.media_type || 'text/plain';
  const card = document.createElement('div');
  card.className = 'artifact';
  const header = document.createElement('header');
  const title = document.createElement('b');
  title.textContent = name;
  const dl = document.createElement('a');
  dl.href = '#';
  dl.textContent = '下载';
  dl.onclick = (e) => {
    e.preventDefault();
    const blob = new Blob([typeof content === 'string' ? content : JSON.stringify(content, null, 2)], { type: mediaType });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = String(name).split('/').pop() || 'artifact';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  };
  header.appendChild(title);
  header.appendChild(dl);
  card.appendChild(header);
  const meta = document.createElement('div');
  meta.className = 'hint';
  meta.textContent = `${ts()} · ${mediaType}`;
  card.appendChild(meta);
  if (content !== '' && content != null) {
    const pre = document.createElement('pre');
    pre.textContent = trunc(content, 3000);
    card.appendChild(pre);
  }
  list.prepend(card);
}

// ---------------- canvas (graph-editor.js) ----------------
let editor = null;
const canvasDoc = { steps: [], metadata: { step_labels: {} } };

function initCanvas() {
  callSeq = 0;
  callNode.clear();
  canvasDoc.steps = [];
  canvasDoc.metadata = { step_labels: {} };
  const root = $('canvasRoot');
  root.innerHTML = '';
  editor = graphEditor({
    root,
    read: () => structuredClone(canvasDoc),
    write: () => {}, // 运行视图只做观察，不持久化用户编辑
    tools: () => [],
    models: () => [],
    flash,
    upload: async () => ({}),
  });
  editor.render(structuredClone(canvasDoc));
}

function refreshCanvas() {
  if (editor) editor.render(structuredClone(canvasDoc));
}

// ---------------- steer ----------------
// SPEC §8: POST /runs/{id}/steer {action, message?}
async function steer(action, message) {
  if (!runId) return flash('还没有运行中的 run');
  const body = { action };
  if (message) body.message = message;
  try {
    const res = await fetch(`/runs/${encodeURIComponent(runId)}/steer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) return flash('steer 失败：HTTP ' + res.status);
    flash(`steer 已发送：${action}`);
    if (action === 'pause') setStatus('paused');
    if (action === 'resume') pollRunState();
  } catch (err) {
    flash('steer 失败：' + err.message);
  }
}

document.querySelectorAll('[data-steer]').forEach((btn) => {
  btn.addEventListener('click', () => steer(btn.dataset.steer));
});
$('redirectBtn').addEventListener('click', () => {
  const msg = $('redirectMsg').value.trim();
  if (!msg) return flash('请先输入 redirect 指令');
  steer('redirect', msg);
  $('redirectMsg').value = '';
});
