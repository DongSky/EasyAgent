// Incremental activity events and their presentation; no conversation mutations.
import { toolLabels } from '../ui-labels.js';

export function createActivityView({ api, escape }) {
  const noiseKinds = new Set(['run.status']);
  const activityLabels = {
    'tool.started': '调用',
    'tool.succeeded': '完成',
    'tool.failed_observed': '失败并反馈给助手',
    'tool.input_rejected': '参数被拒绝，助手将修正',
    'tool.unknown_requested': '请求了不存在的工具',
    'tool.approval_required': '等待你确认',
    'tool.authorized': '已按自动执行授权',
    'model.started': '思考中',
    'definition.saved': '已保存定义',
    'run.created': '启动子任务',
    'agent.recovering': '恢复上下文',
    'context.compacted': '压缩历史',
    'context.compaction_failed': '整理上下文失败，改用其他方式继续',
    'input.requested': '等待你补充信息',
    'session.steered': '已接收补充要求',
  };
  function activityHTML(events) {
    const rows = [];
    for (const ev of events) {
      const p = ev.payload || {},
        kind = ev.kind;
      let text = null;
      if (noiseKinds.has(kind)) continue;
      if (kind === 'tool.started') text = '调用 ' + (toolLabels[p.tool] || p.tool);
      else if (kind === 'tool.succeeded') text = '✓ ' + (toolLabels[p.tool] || p.tool);
      else if (kind === 'tool.failed_observed' || kind === 'tool.input_rejected')
        text =
          '✗ ' +
          (toolLabels[p.tool] || p.tool) +
          ' · ' +
          (kind === 'tool.failed_observed'
            ? '失败（' + (p.code || '') + '），已反馈给助手修正'
            : '参数无效，助手将修正');
      else if (kind === 'tool.unknown_requested')
        text = '✗ 请求了不存在的工具 ' + p.tool + '，已告知助手';
      else if (kind === 'tool.approval_required')
        text = 'Ⅱ 等待你确认 ' + (toolLabels[p.tool] || p.tool);
      else if (kind === 'definition.saved')
        text =
          '💾 已保存' +
          ({ workflow: '流程', api: '接口节点', node: '节点', component: '组件' }[p.kind] ||
            p.kind) +
          ' ' +
          p.id +
          ' v' +
          p.revision;
      else if (kind === 'model.started') text = '… 思考中';
      else if (kind === 'agent.recovering')
        text =
          '↻ ' +
          (p.reason === 'context_overflow'
            ? '上下文已满，已压缩后继续'
            : '输出被截断，已要求分步继续');
      else if (kind === 'input.requested') text = 'Ⅱ 等待你补充：' + (p.prompt || '');
      else if (kind === 'context.compaction_failed') text = '! ' + (activityLabels[kind] || kind);
      else if (kind === 'session.steered') text = '✦ 已接收你的补充要求';
      if (text) rows.push({ text, kind, time: ev.created });
    }
    const shown = rows.slice(-40);
    return (
      shown
        .map(
          (r) =>
            `<div class="chat-activity-row kind-${escape(r.kind.replace(/\./g, '-'))}"><small>${new Date(r.time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</small><span>${escape(r.text)}</span></div>`
        )
        .join('') +
      (rows.length > 40 ? `<p class="muted">仅显示最近 40 条，共 ${rows.length} 条</p>` : '')
    );
  }
  async function updateActivity(entry, run) {
    const slot = entry.root.querySelector('[data-activity]');
    if (!run?.spec?.metadata?.operator) {
      slot.hidden = true;
      return;
    }
    if (entry.eventRun !== run.id) {
      entry.eventRun = run.id;
      entry.events = [];
      entry.eventCursor = 0;
    }
    const fresh = await api(`/v1/runs/${run.id}/events?after=${entry.eventCursor}`);
    for (const ev of fresh) {
      entry.eventCursor = Math.max(entry.eventCursor, ev.id || 0);
      entry.events.push(ev);
    }
    if (!fresh.length && slot.dataset.rendered === String(entry.eventCursor)) return;
    slot.dataset.rendered = String(entry.eventCursor);
    slot.hidden = false;
    const running = ['queued', 'running'].includes(run.status);
    slot.innerHTML = `<details ${running || !slot.dataset.userClosed ? 'open' : ''}><summary>活动记录 · ${entry.events.filter((e) => e.kind === 'tool.succeeded').length} 次工具调用${running ? ' · 进行中' : ''}</summary><div class="chat-activity-list">${activityHTML(entry.events)}</div></details>`;
    slot.querySelector('details').ontoggle = (e) => {
      slot.dataset.userClosed = e.target.open ? '' : '1';
    };
    if (running) {
      const list = slot.querySelector('.chat-activity-list');
      list.scrollTop = list.scrollHeight;
    }
  }
  return updateActivity;
}
