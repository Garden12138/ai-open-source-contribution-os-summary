"""Deterministic initial environment support, with explicit unsupported cases."""
from __future__ import annotations
from typing import Any
from pathlib import PurePosixPath


def environment_plan(paths: list[str], *, change_paths: list[str] | None = None) -> dict[str, Any]:
    paths = set(paths)
    # Select the closest package for actual code changes, not every ecosystem
    # present elsewhere in a monorepo. No repository code is evaluated here.
    if change_paths:
        roots = set()
        for path in change_paths:
            if PurePosixPath(path).suffix not in {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx"}:
                continue
            for parent in PurePosixPath(path).parents:
                prefix = "" if str(parent) == "." else str(parent) + "/"
                if prefix + "pyproject.toml" in paths or prefix + "package.json" in paths:
                    roots.add(prefix)
                    break
        if len(roots) > 1:
            return {"status": "unsupported", "manifests": [], "reason": "当前改动跨多个包，需拆分方案或提供跨包环境适配。"}
        if roots:
            prefix = roots.pop()
            scoped = {p[len(prefix):] for p in paths if p.startswith(prefix) and "/" not in p[len(prefix):]}
            if "pyproject.toml" in scoped:
                manifests = [prefix + "pyproject.toml"]
                manifests += [prefix + p for p in ("test-requirements.txt", "requirements.txt") if prefix + p in paths]
                if prefix + "requirements/pytest.txt" in paths:
                    manifests.append(prefix + "requirements/pytest.txt")
                for parent in PurePosixPath(prefix or ".").parents:
                    p = ("" if str(parent) == "." else str(parent) + "/") + "dev-requirements.txt"
                    if p in paths:
                        manifests.append(p)
                        break
                return {"status": "supported", "ecosystem": "python", "mode": "workspace-v2",
                        "project_directory": prefix.rstrip("/") or ".", "manifests": manifests,
                        "reason": "在隔离环境中解析所选 Python 包与测试依赖，冻结 wheel 哈希；先运行基线，再实施及离线验证。"}
            selected = environment_plan(list(scoped))
            if selected["status"] == "supported":
                selected.update(mode="workspace-v2", project_directory=prefix.rstrip("/") or ".",
                                manifests=[prefix + p for p in selected["manifests"]])
            return selected
    if any("/" in p and PurePosixPath(p).name in {"pyproject.toml", "package.json"} for p in paths):
        return {"status": "pending_scope", "manifests": [], "reason": "仓库包含多个子包；保存具体改动范围后选择对应测试环境。"}
    python = bool(paths & {"requirements.lock", "requirements.txt", "pyproject.toml", "setup.py", "Pipfile"})
    node = "package.json" in paths
    if python and node:
        return {"status": "unsupported", "reason": "项目同时需要 Python 和 Node 环境，需专用环境适配。", "manifests": []}
    if node:
        if "package-lock.json" not in paths:
            return {"status": "unsupported", "reason": "Node 环境需要 package-lock.json；当前锁文件尚未适配。", "manifests": []}
        return {"status": "supported", "ecosystem": "node", "manifests": ["package.json", "package-lock.json"],
                "reason": "通过包代理准备锁定依赖，禁用安装脚本，随后离线验证。"}
    if python:
        lock = next((p for p in ("requirements.lock", "requirements.txt") if p in paths), None)
        if not lock:
            return {"status": "unsupported", "reason": "Python 环境需要包含版本及 SHA-256 的 requirements.lock 或 requirements.txt。", "manifests": []}
        return {"status": "supported", "ecosystem": "python", "manifests": [lock],
                "reason": "仅安装带哈希的锁定 wheel；缺少哈希或需源码构建时停止环境准备。"}
    return {"status": "builtin", "manifests": [], "reason": "使用 Runner 内置工具；验证会明确报告缺少的依赖。"}


def task_environment(wb: Any, task_id: str) -> dict[str, Any]:
    context = wb.latest(task_id, "context_ready")
    if context is None:
        from app.jobs import TERMINAL_STATES
        reads = [j for j in wb.jobs(task_id) if j.kind in {"planning_archive", "planning_context"}]
        active = next((j for j in reversed(reads) if j.state not in TERMINAL_STATES), None)
        if active:
            reason = "仓库读取已排队，等待后台处理。" if active.state == "queued" else "正在读取仓库代码。"
        elif reads:
            reason = {
                "failed": "上次读取仓库失败；发送“继续”重试。",
                "timed_out": "上次读取仓库超时；发送“继续”重试。",
                "cancelled": "仓库读取已停止；发送“继续”恢复。",
            }.get(reads[-1].state, "仓库上下文尚未就绪；发送“继续”重新读取。")
        else:
            reason = "尚未读取仓库；发送要求或点击“读取仓库并准备方案”开始。"
        return {"status": "pending", "manifests": [], "reason": reason}
    evidence = wb.read(context.payload["artifact_id"])
    plan = wb.latest_plan(task_id)
    if plan is None:
        return {"status": "pending_scope", "manifests": [],
                "reason": "仓库代码已读取；先阅读相关代码与项目规范、确定方案，再评估实施与测试依赖。"}
    from app.execution_preflight import inspect_environment
    selected = environment_plan(evidence.get("inventory", []), change_paths=list(plan.files_likely_to_change))
    from app.config import dependency_builder_image
    builder = dependency_builder_image()
    if builder and selected.get("mode") == "workspace-v2" and selected.get("ecosystem") == "python":
        selected = {**selected,"distribution_strategy":"source-wheel-v1","builder_image_digest":builder}
    return inspect_environment(selected, evidence)


def schedule_verification(wb: Any, attempt_id: str, signer: Any) -> None:
    from app.executions import ExecutionAttemptService
    from app.execution_control import ExecutionStageControlService
    from app.plans import PlanVersionService
    from app.changesets import plan_sandbox_commands
    from app.sandbox_worker.specs import SandboxPolicy
    from app.workbench import WorkbenchError
    attempts = ExecutionAttemptService(wb.session)
    attempt = attempts.get_verified(attempt_id)
    current = attempts.current(attempt_id)
    if current.stage != "implement" or current.status != "succeeded":
        return
    result = wb.read(current.result_hash)
    approved = PlanVersionService(wb.session).get_verified(attempt.plan_version_id)
    selected = environment_plan([f["path"] for f in result["result_inventory"]], change_paths=list(approved.files_likely_to_change))
    inputs = [current.result_hash]
    if selected["status"] == "unsupported":
        raise WorkbenchError(selected["reason"], code="environment_unsupported")
    if selected["status"] == "supported":
        receipt = next((e for e in reversed(wb.history(attempt.task_id)) if e.kind == "dependencies_ready"
                        and e.payload["attempt_id"] == attempt_id), None)
        if receipt is None:
            if selected.get("mode") == "workspace-v2":
                raise WorkbenchError("缺少修改前冻结的子包环境，请重新确认方案执行。", code="environment_stale")
            wb.enqueue(attempt.task_id, "execution_dependencies", {"execution_attempt_id": attempt_id,
                "implement_result_hash": current.result_hash}, key="dependencies:" + attempt_id)
            return
        dependency_value = wb.read(receipt.payload["result_hash"])
        if dependency_value.get("source_kind") == "explore":
            from app.sandbox_worker.workspace_jobs import preparation_receipt
            baseline = preparation_receipt(wb, attempt_id)
            if baseline is None:
                raise WorkbenchError("缺少修改前基线，不能开始验证。", code="environment_stale")
            baseline_value = wb.read(baseline.payload["result_hash"])
            environment_spec = wb.read(baseline_value["environment_spec_hash"])
            actual = {f["path"]: f.get("sha256") for f in result["result_inventory"]}
            if any(actual.get(m["path"]) != m["content_hash"] for m in environment_spec["manifests"]):
                raise WorkbenchError("修改影响了冻结依赖清单，需新方案与环境。", code="environment_stale")
        inputs.append(receipt.payload["result_hash"])
    pending = attempts.advance_stage(attempt_id, expected_sequence=current.sequence,
        expected_record_hash=current.record_hash, input_hashes=tuple(inputs), reason_code="implement_succeeded",
        idempotency_key=f"advance:verify:{attempt_id}:{current.sequence}")
    commands = plan_sandbox_commands(PlanVersionService(wb.session).get_verified(attempt.plan_version_id))
    spec = attempts.build_current_job_spec(attempt_id, commands=commands)
    ExecutionStageControlService(wb.session).schedule_current(attempt_id, signed_job_spec=signer.sign(spec),
        signer=signer, sandbox_policy=SandboxPolicy(), idempotency_key=f"schedule:{attempt_id}:{pending.stage}:{pending.sequence}")
