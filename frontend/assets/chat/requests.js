// Run polling policy. The controller owns the cache and checks conversation identity after awaits.
export async function readProgress(api, cache, runId, turnStatus) {
  let run = cache.get(runId);
  if (
    !run ||
    !['succeeded', 'failed', 'cancelled'].includes(run.status) ||
    ['starting', 'running'].includes(turnStatus)
  ) {
    run = await api('/v1/runs/' + runId + '?progress=true');
    if (['succeeded', 'failed', 'cancelled'].includes(run.status)) {
      run = await api('/v1/runs/' + runId);
    }
    cache.set(run.id, run);
  }
  return run;
}

export function messagePayload(text, attachments, destination, execution, workingTurn) {
  const builtin = ['auto', 'create', 'chat'].includes(destination);
  const payload = {
    text,
    attachments,
    execution,
    intent: builtin ? destination : 'workflow',
    ...(!builtin ? { workflow: destination } : {}),
  };
  if (workingTurn && destination === 'auto') payload.mode = 'steer';
  return payload;
}
