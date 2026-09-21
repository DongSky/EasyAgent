export function stepStatus(run, step, statuses) {
  if (step.status === 'retrying') return '等待自动重试';
  if (step.status === 'running') {
    if (step.build_turns != null) return `正在分段构建 · 第 ${step.build_turns} 轮`;
    const segments = Object.values(step.state?.build_requests || {})
      .map((r) => r.segments)
      .find((s) => s && !s.complete);
    if (segments) return `正在分段构建 · 第 ${segments.turns} 轮`;
  }
  if (step.status === 'waiting_children') {
    const children = (run.children || []).filter((child) => child.step_id === step.id);
    const blocking = [
      ['needs_attention', '子流程结果不明，需核验'],
      ['waiting_approval', '子流程等待确认执行'],
      ['waiting_input', '子流程等待补充信息'],
      ['failed', '子流程处理失败'],
      ['waiting_remote', '子流程等待外部任务结果'],
    ];
    for (const [status, label] of blocking)
      if (children.some((child) => child.status === status)) return label;
    return '等待子流程完成';
  }
  if (step.status === 'queued') {
    const waiting = (step.spec?.depends_on || [])
      .map((id) => run.steps.find((s) => s.id === id))
      .filter((s) => s && s.status !== 'succeeded');
    if (waiting.length) {
      const labels = run.spec?.metadata?.step_labels || {};
      return '等待前置步骤：' + waiting.map((s) => labels[s.id] || s.id).join('、');
    }
  }
  return statuses[step.status] || step.status;
}
