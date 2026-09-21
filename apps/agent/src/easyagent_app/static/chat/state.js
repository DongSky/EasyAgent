// Derived display state. Backend phases remain the source of truth.
export function conversationState(conversation) {
  const turnBusy =
    !!conversation.active_run ||
    conversation.turns.some(
      (turn) =>
        !['succeeded', 'failed', 'cancelled', 'waiting_connections', 'steered'].includes(
          turn.status
        )
    );
  const workingTurn =
    conversation.turns.find(
      (turn) =>
        ['starting', 'running'].includes(turn.status) &&
        conversation.phases.operator.some(
          (phase) => turn.task?.phase === phase || turn.task?.phase === phase + '_starting'
        )
    ) || null;
  return { chosenModel: conversation.model, turnBusy, workingTurn };
}
