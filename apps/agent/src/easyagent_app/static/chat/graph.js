// Execution graph rendering only; requests and conversation state stay in the controller.
import { stepStatus } from '../run-status.js?v=20260921-readable-1';
import { toolLabels } from '../ui-labels.js';

export function createRunGraph({ escape, statuses, guard, showDetails, reducedMotion }) {
  function graphHTML(run) {
    const steps = run.steps,
      byId = new Map(steps.map((s) => [s.id, s])),
      levels = new Map();
    function depth(s) {
      if (levels.has(s.id)) return levels.get(s.id);
      const d = Math.max(-1, ...s.spec.depends_on.map((id) => depth(byId.get(id)))) + 1;
      levels.set(s.id, d);
      return d;
    }
    steps.forEach(depth);
    const vertical = innerWidth <= 760,
      columns = new Map(),
      positions = new Map();
    for (const s of steps) {
      const d = levels.get(s.id),
        row = columns.get(d) || [];
      positions.set(s.id, {
        x: (vertical ? row.length : d) * 224 + 10,
        y: (vertical ? d : row.length) * 91 + 12,
      });
      row.push(s);
      columns.set(d, row);
    }
    const breadth = Math.max(...[...columns.values()].map((a) => a.length)),
      width = Math.max(240, (vertical ? breadth : columns.size) * 224),
      height = (vertical ? columns.size : breadth) * 91 + 12;
    const lines = steps
      .flatMap((s) =>
        s.spec.depends_on.map((id) => {
          const a = positions.get(id),
            b = positions.get(s.id);
          const d = vertical
            ? `M${a.x + 96},${a.y + 63} C${a.x + 96},${a.y + 78} ${b.x + 96},${b.y - 15} ${b.x + 96},${b.y}`
            : `M${a.x + 192},${a.y + 31} C${a.x + 212},${a.y + 31} ${b.x - 20},${b.y + 31} ${b.x},${b.y + 31}`;
          return `<g class="chat-edge" data-from="${escape(id)}" data-to="${escape(s.id)}"><path class="chat-edge-track" d="${d}"/><path class="chat-edge-flow" d="${d}"/></g>`;
        })
      )
      .join('');
    const labels = run.spec.metadata.step_labels || {},
      kinds = {
        model: '模型处理',
        agent: '智能处理',
        artifact: '保存结果',
        transform: '整理数据',
        input: '补充信息',
        approval: '确认操作',
        retrieve: '检索资料',
        subworkflow: '子流程',
        foreach: '批量处理',
      };
    return `<div class="chat-dag-scroll" tabindex="0" aria-label="工作流实时执行图"><div class="chat-dag" style="width:${width}px;height:${height}px"><svg viewBox="0 0 ${width} ${height}" aria-hidden="true">${lines}</svg>${steps
      .map((s) => {
        const p = positions.get(s.id);
        return `<button class="chat-node" style="left:${p.x}px;top:${p.y}px" data-node="${escape(s.id)}"><span class="chat-node-indicator" aria-hidden="true"></span><div><b>${escape(labels[s.id] || toolLabels[s.spec.target] || kinds[s.spec.kind] || s.spec.target || s.id)}</b><small></small></div></button>`;
      })
      .join('')}</div></div>`;
  }
  function updateGraph(entry, run) {
    const graph = entry.root.querySelector('[data-graph]');
    // Keep existing nodes and SVG paths while states change: preserve focus,
    // scrolling and animation timelines across the 800 ms polling interval.
    const key = JSON.stringify([
      run.id,
      innerWidth <= 760,
      run.steps.map((s) => [s.id, s.spec.depends_on]),
    ]);
    const rebuilt = key !== entry.lastGraph;
    if (rebuilt) {
      entry.lastGraph = key;
      graph.innerHTML = graphHTML(run);
      graph.querySelectorAll('[data-node]').forEach((button) => {
        button.onclick = guard(async () => {
          entry.details.hidden = false;
          const toggle = entry.root.querySelector('[data-details-toggle]');
          toggle.textContent = '收起步骤与结果';
          toggle.setAttribute('aria-expanded', 'true');
          await showDetails(entry);
          entry.details.scrollIntoView({
            block: 'nearest',
            behavior: reducedMotion.matches ? 'auto' : 'smooth',
          });
        });
        button.addEventListener('animationend', (e) => {
          if (e.animationName === 'chatNodeComplete') button.classList.remove('just-completed');
        });
      });
    }
    const terminal = ['succeeded', 'failed', 'cancelled'].includes(run.status);
    const byId = new Map(run.steps.map((s) => [s.id, s]));
    const statusOf = (step) => (terminal && step.status === 'running' ? run.status : step.status);
    const icons = {
      running: '◌',
      retrying: '↻',
      succeeded: '✓',
      failed: '!',
      waiting_approval: 'Ⅱ',
      waiting_input: 'Ⅱ',
      needs_attention: '!',
      waiting_remote: '…',
      waiting_children: '…',
      cancelled: '−',
      skipped: '−',
    };
    graph.querySelectorAll('[data-node]').forEach((button) => {
      const step = byId.get(button.dataset.node),
        status = statusOf(step),
        previous = button.dataset.status;
      const label = stepStatus(run, { ...step, status }, statuses);
      button.querySelector('small').textContent = label;
      button.setAttribute('aria-label', button.querySelector('b').textContent + ' · ' + label);
      if (previous === status) return;
      button.dataset.status = status;
      button.className = 'chat-node state-' + status;
      button.querySelector('.chat-node-indicator').textContent = icons[status] || '·';
      if (!rebuilt && previous && status === 'succeeded' && !reducedMotion.matches)
        button.classList.add('just-completed');
    });
    graph.querySelectorAll('.chat-edge').forEach((edge) => {
      const from = statusOf(byId.get(edge.dataset.from)),
        to = statusOf(byId.get(edge.dataset.to));
      let status = 'pending';
      if (['cancelled', 'skipped'].includes(to)) status = 'inactive';
      else if (to === 'failed') status = 'failed';
      else if (['waiting_input', 'waiting_approval', 'needs_attention'].includes(to))
        status = 'waiting';
      else if (from === 'succeeded' && to === 'running' && !terminal) status = 'running';
      else if (from === 'succeeded' && to === 'succeeded') status = 'succeeded';
      if (edge.dataset.status !== status) {
        edge.dataset.status = status;
        edge.setAttribute('class', 'chat-edge state-' + status);
      }
    });
  }
  return updateGraph;
}
