from contribos.agent.engine import AgentCapabilities, EngineEvent, TaskContext


def test_only_completed_events_are_persistent():
    for kind in ("message.delta", "reasoning.delta"):
        assert not EngineEvent(kind).persistent
    for kind in ("message.agent", "reasoning.summary", "command.completed", "turn.completed"):
        assert EngineEvent(kind).persistent


def test_context_defaults_do_not_share_mutable_state():
    a = TaskContext("one", "owner/repo", 1, "/repo", "/context")
    b = TaskContext("two", "owner/repo", 2, "/repo", "/context")
    a.issue["title"] = "Task one"
    assert not b.issue
    assert not a.trust_confirmed
    assert not AgentCapabilities().load_session
