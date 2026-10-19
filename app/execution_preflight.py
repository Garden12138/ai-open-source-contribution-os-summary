"""Content-based execution admission, shared with isolated preparation.

This only inspects frozen data. It never evaluates repository code or prepares
dependencies. Passing admission is not proof that installation/tests succeeded.
"""
from __future__ import annotations

import hashlib
import tomllib
from typing import Any

from app.context_limits import MAX_CODING_FILE_BYTES, MAX_CODING_CONTEXT_BYTES
from app.provenance import canonical_json, content_hash

VERSION = "execution-capabilities-v3"
PYTHON_PREPARATION_VERSION = "python-workspace-preparation-v2"


def python_development_requirements(metadata: dict) -> list[str]:
    """Read bounded test/development groups, never evaluate group contents."""
    groups = metadata.get("dependency-groups", {})
    if not isinstance(groups, dict):
        raise ValueError("dependency_groups_unsupported")
    requirements = []
    for name in ("dev", "test", "tests"):
        values = groups.get(name, [])
        if not isinstance(values, list) or any(python_requirement_error(value) for value in values):
            raise ValueError("dependency_groups_unsupported")
        requirements.extend(values)
    if len(requirements) > 300:
        raise ValueError("dependency_groups_unsupported")
    return requirements


def environment_inputs_hash(context: Any, selected: dict[str, Any]) -> str:
    """Same frozen environment across plan edits has the same failure boundary."""
    return content_hash({"version":VERSION, "archive_hash":context.payload["archive_hash"],
        "image":context.payload["image"],"policy_hash":context.payload["policy_hash"],"environment":selected})


def python_requirement_error(requirement: object) -> str | None:
    import re
    if (not isinstance(requirement, str) or len(requirement) > 2000
            or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[|\s|[<>=!~;]|$)", requirement)
            or any(char in requirement for char in ("@", "/", "\\", "\n", "\r", "\x00"))):
        return "requirement_unsupported"
    return None


def python_backend_error(metadata: dict) -> str | None:
    import re
    build = metadata.get("build-system", {})
    if not isinstance(build, dict):
        return "build_metadata_unsupported"
    backend, paths = build.get("build-backend"), build.get("backend-path", [])
    if (not isinstance(backend, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?::[A-Za-z_]\w*)?", backend)
            or not isinstance(paths, list) or any(not isinstance(path, str) or not path or path.startswith("/")
                or ".." in path.split("/") or "\\" in path for path in paths)):
        return "build_metadata_unsupported"
    return None


def python_project_error(metadata: dict, *, allow_dynamic: bool = False, include_development: bool = False) -> str | None:
    """Keep this self-contained: the same function runs in the trusted Runner program."""
    project = metadata.get("project", {})
    if not isinstance(project, dict) or not isinstance(project.get("name"), str) or not project["name"].strip():
        return "project_metadata_missing"
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or any(not isinstance(value, str) for value in dynamic):
        return "project_metadata_invalid"
    if set(dynamic) & {"dependencies", "optional-dependencies"} and not allow_dynamic:
        return "dynamic_dependencies"
    if set(dynamic) & {"dependencies", "optional-dependencies"} and python_backend_error(metadata):
        return "build_metadata_unsupported"
    if not isinstance(project.get("dependencies", []), list):
        return "project_metadata_invalid"
    extras = project.get("optional-dependencies", {})
    build = metadata.get("build-system", {})
    if not isinstance(extras, dict) or not isinstance(build, dict) or not isinstance(build.get("requires", []), list):
        return "project_metadata_invalid"
    groups = [project.get("dependencies", []), build.get("requires", [])]
    for name in ("test", "tests", "instruments", "type-check"):
        if not isinstance(extras.get(name, []), list):
            return "project_metadata_invalid"
        groups.append(extras.get(name, []))
    if any(python_requirement_error(item) for group in groups for item in group):
        return "requirement_unsupported"
    if include_development:
        try:
            python_development_requirements(metadata)
        except ValueError:
            return "dependency_groups_unsupported"
    return None


def inspect_environment(selected: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """A recognized filename selects an adapter; inspected content admits it."""
    if selected.get("status") != "supported" or selected.get("mode") != "workspace-v2" or selected.get("ecosystem") != "python":
        return selected
    selected = {**selected, "preparation_version": PYTHON_PREPARATION_VERSION}
    entries = {entry["path"]: entry for entry in evidence["entries"]}
    missing = []
    reasons = []
    hashes = {}
    for path in selected["manifests"]:
        entry = entries.get(path)
        if entry is None:
            missing.append(path)
            continue
        text = entry.get("content")
        if text is None:
            reasons.append("manifest_unavailable")
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest != entry.get("prior_hash"):
            reasons.append("manifest_hash_mismatch")
            continue
        hashes[path] = digest
        if path.endswith("pyproject.toml"):
            try:
                metadata = tomllib.loads(text)
                error = python_project_error(metadata, include_development=True)
                if error == "dynamic_dependencies":
                    error = python_project_error(metadata, allow_dynamic=True, include_development=True)
                    if error is None:
                        selected = {**selected, "metadata_strategy":"wheel-metadata-v1"}
            except (ValueError, TypeError):
                error = "project_metadata_invalid"
            if error:
                reasons.append(error)
        elif path.endswith((".txt", ".lock")):
            requirements = [line.split(" #", 1)[0].strip() for line in text.splitlines()
                            if line.strip() and not line.lstrip().startswith("#")]
            if any(python_requirement_error(value) for value in requirements):
                reasons.append("requirement_unsupported")
    if reasons:
        messages = {
            "dependency_groups_unsupported": "开发和测试依赖组包含不受支持的声明，需补充隔离准备适配；不会跳过这些依赖开始实施。",
            "dynamic_dependencies": "项目使用动态依赖声明，当前隔离准备器尚未适配；源码可以阅读、方案可以保存，但不能启动实施。需要补充执行适配，重复确认或重试不会解决。",
            "build_metadata_unsupported": "动态依赖的构建后端声明缺失或含不安全路径，不能启动隔离元数据准备。",
            "project_metadata_missing": "项目缺少当前准备器要求的声明式包元数据，尚不能启动实施；需要补充执行适配。",
            "project_metadata_invalid": "项目依赖元数据未通过当前准备器校验，尚不能启动实施。",
            "manifest_unavailable": "依赖清单原文不可用，不能确认执行条件。",
            "manifest_hash_mismatch": "依赖清单内容与冻结哈希不一致，不能启动执行。",
            "requirement_unsupported": "依赖声明含当前准备器不支持的安装选项、路径或 URL，不能启动实施；需要先补充受限依赖适配。",
        }
        return {**selected, "status":"unsupported", "reason_code":reasons[0], "reason":messages[reasons[0]],
                "manifest_hashes":hashes}
    if missing:
        return {**selected, "status":"pending_read", "required_paths":missing,
                "reason":"执行所需的依赖清单尚未读取；先补读并校验项目配置，再确认方案。"}
    return {**selected, "manifest_hashes":hashes, "capability_version":VERSION}


def task_preflight(wb: Any, task_id: str, *, environment: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.execution_environment import task_environment
    plan = wb.latest_plan(task_id)
    context = wb.latest(task_id, "context_ready")
    selected = environment if environment is not None else task_environment(wb, task_id)
    issues = []
    if plan is not None and context is not None:
        evidence = wb.read(context.payload["artifact_id"])
        entries = {entry["path"]: entry for entry in evidence["entries"]}
        known = set(evidence["inventory"])
        environment_hash = environment_inputs_hash(context,selected)
        original_environment_hash = environment_inputs_hash(context, {k:v for k,v in selected.items() if k != "native_resource"})
        events = wb.history(task_id)
        resolved = {e.payload.get("diagnostic_hash") for e in events
                    if e.kind == "execution_environment_diagnostic_resolved" and e.actor_id == "system"
                    and e.payload.get("version") == "package-dns-transport-v1"
                    and e.payload.get("environment_inputs_hash") == environment_hash}
        diagnosis = next((e for e in reversed(events)
            if e.kind in {"execution_preparation_failed","execution_environment_diagnostic"}
            and e.record_hash not in resolved
            and (e.payload.get("environment_inputs_hash") == environment_hash or (
                e.payload.get("environment_inputs_hash") == original_environment_hash
                and e.payload.get("diagnostic",{}).get("reason_code") != "native_tool_unavailable"))),None)
        if diagnosis and diagnosis.payload.get("diagnostic",{}):
            diagnostic = diagnosis.payload["diagnostic"]
            if diagnostic.get("reason_code") in {"no_matching_distribution","dependency_conflict","proxy_denied","native_tool_unavailable"}:
                from app.sandbox_worker.dependency_diagnostics import dependency_diagnostic_message
                issues.append({"code":"environment_known_failure", "stage":"prepare", "paths":[],
                    "message":dependency_diagnostic_message(diagnostic)+"已保存相同冻结环境的失败证据，重复确认不会解决；需先完成环境适配或显式重新诊断。",
                    "reason_code":diagnostic["reason_code"],"diagnostic_hash":diagnosis.record_hash,
                    "diagnostic_artifact_id":diagnosis.payload["result_hash"]})
        if selected["status"] not in {"supported", "builtin"}:
            issues.append({"code":"environment_unsupported" if selected["status"] == "unsupported" else "environment_evidence_pending",
                           "stage":"prepare", "message":selected["reason"], "reason_code":selected.get("reason_code"),
                           "paths":selected.get("required_paths", [])})
        paths = sorted(set(plan.files_to_inspect) | set(plan.files_likely_to_change))
        total = 0
        for path in paths:
            entry = entries.get(path)
            text = entry.get("content") if entry else None
            if text is None:
                if path in known:
                    issues.append({"code":"coding_evidence_pending", "stage":"coding", "paths":[path],
                                   "message":f"实施前缺少 {path} 的可用原文，请先补读；不能将已有文件当成新文件。"})
                continue
            size = len(text.encode())
            total += len(canonical_json(entry).encode())
            if size > MAX_CODING_FILE_BYTES:
                issues.append({"code":"coding_file_budget_exceeded", "stage":"coding", "paths":[path],
                    "message":f"{path} 可以阅读，但超过当前实施器单文件 {MAX_CODING_FILE_BYTES // 1000} KB 的上下文限制；需要大文件编辑适配，不能直接确认执行。"})
        if total + 16_000 > MAX_CODING_CONTEXT_BYTES:
            issues.append({"code":"coding_context_budget_exceeded", "stage":"coding", "paths":[],
                           "message":"方案涉及的代码超过实施上下文预算，需要拆分方案或补充编辑适配。"})
    report = {"version":VERSION, "status":"not_planned" if plan is None else "blocked" if issues or context is None else "ready",
              "issues":issues, "environment":selected,
              "input_hash":content_hash({"version":VERSION, "plan_hash":plan.record_hash if plan else None,
                                         "context_hash":context.record_hash if context else None}),
              "limits":{"coding_file_bytes":MAX_CODING_FILE_BYTES, "coding_context_bytes":MAX_CODING_CONTEXT_BYTES}}
    return {**report, "report_hash":content_hash(report)}
