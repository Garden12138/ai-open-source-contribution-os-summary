export function timeline() {
  const history = new Map();
  const live = new Map();
  let cursor = 0n;

  function clearLive(turnId) {
    for (const [key, event] of live) {
      if (turnId === undefined || event.turn_id === turnId) live.delete(key);
    }
  }

  function receive(event, eventId = event.seq) {
    const { type, payload, turn_id: turn } = event;
    if (type === 'stream.snapshot') {
      clearLive();
      for (const item of payload.items) {
        if (item.type.endsWith('.delta')) receive(item);
      }
      return true;
    }

    if (type.endsWith('.delta')) {
      const key = `${turn}:${type}`;
      const text = (live.get(key)?.payload.text || '') + payload.text;
      live.set(key, { ...event, payload: { ...payload, text } });
      return true;
    }

    const seq = BigInt(eventId);
    if (seq <= cursor) return false;
    cursor = seq;
    if (type === 'message.agent') live.delete(`${turn}:message.delta`);
    if (type === 'reasoning.summary') live.delete(`${turn}:reasoning.delta`);
    if (['turn.completed', 'turn.interrupted', 'turn.failed'].includes(type)) clearLive(turn);

    const request = payload.request_id ? `request:${payload.request_id}` : null;
    if (request && ['permission.resolved', 'question.answered', 'system.request_expired'].includes(type)) {
      const prior = history.get(request);
      if (prior) history.set(request, { ...prior, payload: {
        ...prior.payload, resolution: type, cancelled: payload.cancelled,
      } });
      return true;
    }

    const toolGroup = type.startsWith('command.') ? 'command' : type;
    const key = payload.tool_call_id && ['command', 'tool.activity', 'files.changed'].includes(toolGroup)
      ? `${turn}:${toolGroup}:${payload.tool_call_id}`
      : (request || `event:${seq}`);
    const previous = history.get(key);
    history.set(key, previous ? { ...event, payload: { ...previous.payload, ...payload } } : event);
    return true;
  }

  return { history, live, receive, clearLive };
}
