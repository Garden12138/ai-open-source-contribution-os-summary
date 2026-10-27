"""Provider failures retain their safe code and retry policy at the coding boundary."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.contribution_workflow import advance_workflows, retry_execution_job
from app.jobs import JobTransitionError
from app.providers.contracts import ProviderRunError
from app.workbench import Workbench
from tests.test_coding_source_reads import direct_fixture


@pytest.mark.parametrize("code,retryable,category,action", [
    ("minimax_completion_truncated", False, "budget", "revise_output_budget"),
    ("model_connection_failed", True, "transport", "retry"),
    ("model_gateway_auth_failure", False, "platform", "inspect_diagnostic"),
    ("model_gateway_rate_limited", True, "transport", "retry"),
])
def test_provider_code_safe_message_and_retryability_survive_job_failure(
    tmp_path, monkeypatch, caplog, code, retryable, category, action
):
    db,s,task,workers,runner,_ = direct_fixture(tmp_path, monkeypatch)
    for _ in range(8):
        for i,worker in enumerate(workers):
            if i != 2:
                job=asyncio.run(worker.run_once())
                if job:assert job.state=="succeeded",job.error_message
        advance_workflows(db,s)
        with db.session() as session:
            w=Workbench(session,s.artifact_root)
            queued=next((j for j in w.jobs(task) if j.kind=="change_set_proposal" and j.state=="queued"),None)
            if queued:break
    else:raise AssertionError("No coding job")
    error=ProviderRunError(code,"模型服务的脱敏诊断",retryable=retryable)
    error.args=("sk-canary-provider-raw-exception-must-not-leak",)
    class FailedProvider:
        calls=0
        async def complete(self,invocation):
            self.calls+=1
            raise error
    failed_provider=FailedProvider();workers[2].runner=failed_provider
    failed=asyncio.run(workers[2].run_once())
    assert failed.id==queued.id and failed.state=="failed"
    assert failed.error_code==code
    assert failed.error_message=="模型服务的脱敏诊断"
    assert failed_provider.calls==1
    advance_workflows(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        event=w.latest(task,"coding_provider_failed")
        assert event.job_id==failed.id and event.actor_id=="orchestrator"
        assert event.payload["retryable"] is retryable
        blocker=w.active_blocker(task)
        assert blocker.payload["reason_code"]==code
        assert blocker.payload["retryable"] is retryable
        assert blocker.payload["recovery"]["category"]==category
        assert blocker.payload["recovery"]["action"]==action
        assert "sk-canary" not in json.dumps([e.payload for e in w.history(task)])+caplog.text
        assert not w.latest(task,"publication_confirmed")
        original_count=len(w.history(task))
        if retryable:
            restored=retry_execution_job(w,failed.id,s)
            assert restored.id==failed.id and restored.state=="queued"
            assert restored.attempt_count==1 and restored.max_attempts==3
        else:
            with pytest.raises(JobTransitionError,match="禁止直接重试"):
                retry_execution_job(w,failed.id,s)
            assert len(w.history(task))==original_count
            assert all(j.state in {"succeeded","failed","cancelled","timed_out"} for j in w.jobs(task))
    db.close()
