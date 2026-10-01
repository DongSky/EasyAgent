const evidenceText = (value) =>
  value === 'Executable checks and step receipts' ? '已核对输出字段和执行回执' : value;
export function goalResult({ run, root, api, escape, guard, watch, loadWorkflow, showTab }) {
  if (!run.spec?.metadata?.goal) return;
  const state = run.steps[0]?.state || {},
    result = run.steps[0]?.output,
    box = document.createElement('section');
  box.className = 'panel';
  const label =
    result?.goal_status === 'completed'
      ? '已通过结果检查'
      : result?.goal_status === 'incomplete'
        ? '尚未达到目标'
        : state.paused
          ? '已暂停续跑'
          : state.phase === 'need_input'
            ? '需要补充信息'
            : '正在执行与检查';
  box.innerHTML = `<h2>${label}</h2><p>${escape(run.spec.steps[0].input.objective)}</p>${run.spec.steps[0].input.checks?.length ? '<details><summary>结果需要满足</summary><ul>' + run.spec.steps[0].input.checks.map((c) => '<li>' + escape(c.description) + '</li>').join('') + '</ul></details>' : ''}<p class="muted">修改 ${state.revision || 0} 次 · 模型调用 ${run.usage?.model_calls || 0} 次 · 子流程 ${run.usage?.child_runs || 0} 个</p>${state.question ? '<p>' + escape(state.question) + '</p>' : ''}${state.reason ? '<p>修改原因：' + escape(state.reason) + '</p>' : ''}<ol>${(state.history || []).map((h) => `<li>版本 ${h.revision + 1}：${h.verdict.passed ? '通过' : '需要改进'}<p>${escape([evidenceText(h.verdict.evidence), ...(h.verdict.unmet || [])].filter(Boolean).join('；'))}</p></li>`).join('')}</ol><div class="actions"><button data-goal-plan ${state.workflow ? '' : 'disabled'}>查看当前流程</button>${state.saved_workflow ? '<button data-goal-reuse>打开可复用版本</button>' : ''}<button data-goal-pause ${['succeeded', 'cancelled'].includes(run.status) ? 'disabled' : ''}>暂停续跑</button><button data-goal-resume ${['succeeded', 'cancelled'].includes(run.status) ? 'disabled' : ''}>继续</button></div><label>补充要求或反馈<textarea data-goal-feedback placeholder="说明结果哪里不符合要求，或补充任务所需的信息"></textarea></label><button data-goal-send>按反馈继续</button><p class="muted">暂停续跑不会中断已启动的步骤。</p>`;
  root.prepend(box);
  box.querySelector('[data-goal-plan]').onclick = () => {
    loadWorkflow(state.workflow);
    showTab('workflow');
  };
  box.querySelector('[data-goal-reuse]')?.addEventListener(
    'click',
    guard(async () => {
      const saved = await api(
        `/v1/studio/workflows/${state.saved_workflow.id}?revision=${state.saved_workflow.revision}`
      );
      loadWorkflow(saved.workflow || saved);
      showTab('workflow');
    })
  );
  const control = async (action, feedback = '') => {
    await api(`/v1/goals/${run.id}/control`, 'POST', { action, feedback });
    await watch(run.id, root);
  };
  box.querySelector('[data-goal-pause]').onclick = guard(() => control('pause'));
  box.querySelector('[data-goal-resume]').onclick = guard(() => control('resume'));
  box.querySelector('[data-goal-send]').onclick = guard(() => {
    const f = box.querySelector('textarea').value.trim();
    if (!f) throw Error('请填写反馈或补充信息');
    return control('feedback', f);
  });
}
