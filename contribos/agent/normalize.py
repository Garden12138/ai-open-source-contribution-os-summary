"""Normalize ACP updates; only completed text records are durable."""

import json
from collections.abc import Callable
from typing import Any

from contribos.agent.engine import EngineEvent
from contribos.security import redact


def clipped(text: str, limit: int, *, tail: bool = False) -> str:
    data = text.encode('utf-8')
    return (data[-limit:] if tail else data[:limit]).decode('utf-8', errors='ignore')


class Normalizer:
    def __init__(self, emit: Callable[[EngineEvent], None], clean: Callable[[str], str] = redact) -> None:
        self.emit = emit
        self.clean = clean
        self.text_kind: str | None = None
        self.parts: list[str] = []
        self.unsent = ''
        self.tools: dict[str, dict[str, Any]] = {}

    def flush(self) -> None:
        if self.text_kind is not None:
            if self.unsent:
                self.emit(EngineEvent(self.text_kind + '.delta', {'text': self.clean(self.unsent)}))
            text = self.clean(''.join(self.parts))
            event_type = 'message.agent' if self.text_kind == 'message' else 'reasoning.summary'
            if self.text_kind == 'reasoning':
                text = clipped(text, 4096)
            self.emit(EngineEvent(event_type, {'text': text}))
        self.text_kind, self.parts, self.unsent = None, [], ''

    def update(self, data: dict[str, Any]) -> bool:
        """Return true only when a modifying tool has finished."""
        kind = data.get('sessionUpdate', 'unknown')
        if kind in {'agent_message_chunk', 'agent_thought_chunk'}:
            text_kind = 'message' if kind == 'agent_message_chunk' else 'reasoning'
            if self.text_kind != text_kind:
                self.flush()
            self.text_kind = text_kind
            text = data.get('content', {}).get('text', '')
            self.parts.append(text)
            self.unsent += text
            # Do not broadcast partial tokens or Authorization headers across chunks.
            if '\n' in self.unsent:
                complete, self.unsent = self.unsent.rsplit('\n', 1)
                self.emit(EngineEvent(text_kind + '.delta', {'text': self.clean(complete + '\n')}))
            return False
        self.flush()
        if kind in {'tool_call', 'tool_call_update'}:
            tool_id = data['toolCallId']
            previous = self.tools.get(tool_id, {})
            tool = {**previous, **{k: v for k, v in data.items() if v is not None}}
            self.tools[tool_id] = tool
            tool_kind = tool.get('kind', 'other')
            status = tool.get('status', 'pending')
            payload = {'tool_call_id': tool_id, 'title': tool.get('title', ''), 'status': status}
            finished = status in {'completed', 'failed'}
            if tool_kind == 'execute':
                if kind == 'tool_call':
                    raw = tool.get('rawInput') or {}
                    command = raw.get('command', tool.get('title', '')) if isinstance(raw, dict) else str(raw)
                    self.emit(EngineEvent('command.started', {**payload, 'command': command}))
                if finished:
                    output = '\n'.join(c.get('content', {}).get('text', '') for c in tool.get('content') or [])
                    raw_out = tool.get('rawOutput') or {}
                    if not output and raw_out:
                        output = json.dumps(raw_out, ensure_ascii=False)
                    result = {**payload, 'output_tail': clipped(self.clean(output), 16384, tail=True)}
                    if isinstance(raw_out, dict):
                        code = raw_out.get('exit_code', raw_out.get('exitCode'))
                        if isinstance(code, int):
                            result['exit_code'] = code
                    self.emit(EngineEvent('command.completed', result))
            elif tool_kind in {'edit', 'delete', 'move'}:
                paths = [loc['path'] for loc in tool.get('locations') or [] if 'path' in loc]
                paths += [c['path'] for c in tool.get('content') or [] if c.get('type') == 'diff' and 'path' in c]
                self.emit(EngineEvent('files.changed', {**payload, 'paths': list(dict.fromkeys(paths))}))
            else:
                self.emit(EngineEvent('tool.activity', {**payload, 'kind': tool_kind}))
            if finished:
                self.tools.pop(tool_id, None)
            return finished and tool_kind in {'edit', 'delete', 'move'}
        if kind == 'plan':
            self.emit(EngineEvent('plan.updated', {'entries': data.get('entries', [])}))
        elif kind == 'current_mode_update':
            self.emit(EngineEvent('mode.changed', {'mode': data.get('currentModeId')}))
        elif kind == 'usage_update':
            self.emit(EngineEvent('usage.updated', {k: v for k, v in data.items() if k != 'sessionUpdate'}))
        else:
            self.emit(EngineEvent('agent.other', {'type': kind}))
        return False
