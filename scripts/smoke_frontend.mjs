// Offline regression checks for the console's event lifecycle and rendering.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const elements = new Map();
const element = () => ({
  value: '', style: {}, dataset: {}, children: [], textContent: '', innerHTML: '',
  addEventListener() {}, scrollIntoView() {}, querySelector() { return null; },
  appendChild(child) { this.children.push(child); }, prepend(child) { this.children.unshift(child); },
});
const get = (id) => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
const controls = Array.from({ length: 5 }, element);
const streams = [];
const timers = new Map();
let timerId = 0;
let state = { status: 'done', step: 1 };
const ctx = vm.createContext({
  console, structuredClone,
  document: { getElementById: get, createElement: element, querySelectorAll: () => controls },
  localStorage: { getItem: () => '', setItem() {}, removeItem() {} },
  graphEditor: () => ({ render() {} }),
  setInterval: (fn) => { timers.set(++timerId, fn); return timerId; },
  clearInterval: (id) => timers.delete(id),
  setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; },
  clearTimeout: (id) => timers.delete(id),
  fetch: async () => ({ ok: true, json: async () => state }),
  EventSource: class {
    constructor(url) { this.url = url; this.listeners = {}; this.closed = false; streams.push(this); }
    close() { this.closed = true; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
  },
});
const source = readFileSync(new URL('../frontend/app.js', import.meta.url), 'utf8')
  .replace(/^import .*;$/m, '');
vm.runInContext(source, ctx);
const evaluate = (code) => vm.runInContext(code, ctx);
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const event = (seq, type, payload) => {
  const data = JSON.stringify({ seq, type, payload });
  evaluate(`handleRaw('message', ${JSON.stringify(data)})`);
};

evaluate("openRun('fast-run', 'mission')");
await flush();
assert.equal(get('statusBadge').textContent, '完成');
assert.equal(streams[0].closed, false, 'terminal polling must not discard SSE replay');
event(1, 'tool_call', { name: 'file.read', args: { path: 'missing.txt' } });
event(2, 'tool_result', { result: { ok: false, error: 'missing' } });
assert.match(get('eventLog').children.at(-1).innerHTML, /失败/);
assert.match(evaluate('canvasDoc.metadata.step_labels["step-1"]'), /✗/);
event(3, 'thought', { content: '<img src=x onerror=alert(1)>' });
assert.match(get('eventLog').children.at(-1).innerHTML, /&lt;img/);
assert.doesNotMatch(get('eventLog').children.at(-1).innerHTML, /<img/);
event(4, 'artifact', { name: 'summary.txt', content: 'complete' });
event(5, 'status', { status: 'done' });
event(6, 'done', { status: 'done' });
event(7, 'prune_candidates', { candidates: ['unused'] });
assert.equal(get('artifactList').children.length, 1);
assert.equal(streams[0].closed, false);
assert.equal(controls.every((c) => c.disabled), true);
const count = evaluate('eventCount');
event(7, 'prune_candidates', { candidates: ['unused'] });
assert.equal(evaluate('eventCount'), count, 'replayed sequence must be deduplicated');
streams[0].listeners.end();
await flush();
assert.equal(streams[0].closed, true);
assert.match(get('planList').innerHTML, /未提供计划/);

state = { status: 'running', step: 0 };
evaluate("openRun('second-run', 'mission2')");
await flush();
assert.equal(get('bSteps').textContent, 0);
assert.equal(controls.some((c) => c.disabled), false);
const polling = evaluate('pollTimer');
streams.at(-1).onerror();
assert.equal(timers.has(polling), true, 'reconnect must preserve state polling');
const reconnect = timers.get(evaluate('reconnectTimer'));
evaluate("openRun('third-run', 'mission3')");
await flush();
const numberOfStreams = streams.length;
reconnect();
await flush();
assert.equal(streams.length, numberOfStreams, 'old reconnect must not subscribe to a new run');
console.log('PASS frontend: terminal replay, artifacts, failed tools, text escaping, deduplication, controls, polling, stale reconnect');
