"""Bounded, read-only repository conventions and deterministic PR text checks.

Files are data, never executed. Sources are pinned to Git commits and hashed.
Natural-language requirements remain visible for human review; this module does
not pretend that arbitrary CI programs can be evaluated without running them.
"""
from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from urllib.parse import quote

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data, redact_text
from app.workbench import WorkbenchError

VERSION = "repository-publication-requirements-v2"
# Match the existing bounded repository-reading allowance. Workflows count as
# evidence too; a large CI configuration must not silently displace PR rules.
MAX_REQUIREMENTS_BYTES = 700_000
MAX_REQUIREMENT_FILE_BYTES = 256_000
DEFAULT_TYPES = ["feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert"]


async def read_requirements(transport, repository: str, commit: str, changed_paths: list[str]) -> dict:
    """Read local and organization defaults, with hard file/request/byte budgets."""
    sources: list[dict] = []
    requests = 0
    total = 0

    async def get(repo: str, path: str, ref: str):
        nonlocal requests
        requests += 1
        if requests > 96:
            raise WorkbenchError("仓库规范读取超过预算，请缩小范围", code="publication_requirements_budget")
        return await transport.api("GET", f"repos/{repo}/contents/{quote(path, safe='/')}?ref={ref}", missing=True)

    async def file(repo: str, path: str, ref: str, role: str) -> bool:
        nonlocal total
        value = await get(repo, path, ref)
        if value is None:
            return False
        if (not isinstance(value, dict) or value.get("type") != "file"
            or value.get("path") != path or value.get("encoding") != "base64"
            or type(value.get("size")) is not int or value["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{40}", str(value.get("sha", "")))):
            raise WorkbenchError("仓库规范文件不完整或类型不支持", code="publication_requirements_invalid")
        if value["size"] > MAX_REQUIREMENT_FILE_BYTES:
            raise WorkbenchError("单份仓库规范超过读取预算", code="publication_requirements_budget")
        try:
            raw = base64.b64decode("".join(value["content"].split()), validate=True)
            text = raw.decode("utf-8")
        except (KeyError, ValueError, UnicodeError, AttributeError):
            raise WorkbenchError("仓库规范文件无法读取", code="publication_requirements_invalid") from None
        if len(raw) != value["size"] or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != value["sha"]:
            raise WorkbenchError("仓库规范文件哈希不一致", code="publication_requirements_invalid")
        total += len(raw)
        if total > MAX_REQUIREMENTS_BYTES or len(sources) >= 48:
            raise WorkbenchError("仓库规范超过读取预算", code="publication_requirements_budget")
        safe_text = redact_text(text)
        sources.append({"repository": repo, "commit": ref, "path": path, "blob_sha": value["sha"],
                        "role": role, "content": safe_text, "redacted": safe_text != text})
        return True

    async def directory(repo: str, path: str, ref: str, role: str):
        entries = await get(repo, path, ref)
        if entries is None:
            return
        if not isinstance(entries, list) or len(entries) > 100:
            raise WorkbenchError("仓库规范目录不完整", code="publication_requirements_invalid")
        for entry in entries:
            name = entry.get("name", "") if isinstance(entry, dict) else ""
            if not re.fullmatch(r"[A-Za-z0-9_. -]{1,100}", name):
                raise WorkbenchError("仓库规范路径无效", code="publication_requirements_invalid")
            if entry.get("type") == "file" and name.lower().endswith((".md", ".txt") if role == "template" else (".yml", ".yaml")):
                await file(repo, path + "/" + name, ref, role)

    await file(repository, "README.md", commit, "guide")
    for path in ("CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING", "docs/CONTRIBUTING.md", ".github/CONTRIBUTING.md", "CLA.md", ".github/CLA.md"):
        await file(repository, path, commit, "guide")
    for path in (".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md", "PULL_REQUEST_TEMPLATE.md", "pull_request_template.md", "docs/PULL_REQUEST_TEMPLATE.md", "docs/pull_request_template.md"):
        await file(repository, path, commit, "template")
    for path in (".github/PULL_REQUEST_TEMPLATE", ".github/pull_request_template", "docs/PULL_REQUEST_TEMPLATE", "PULL_REQUEST_TEMPLATE"):
        await directory(repository, path, commit, "template")
    await directory(repository, ".github/workflows", commit, "workflow")
    # Package-local contribution rules apply in monorepos, too.
    directories = {"/".join(path.split("/")[:i]) for path in changed_paths for i in range(1, len(path.split("/")))}
    if len(directories) > 24:
        raise WorkbenchError("贡献规范涉及过多目录，请缩小改动范围", code="publication_requirements_budget")
    for path in sorted(directories):
        if path not in {"docs", ".github"}:
            await file(repository, path + "/CONTRIBUTING.md", commit, "guide")
    # Common monorepo development guides linked by contribution instructions.
    for path in sorted({p.split("/")[0] for p in changed_paths if "/" in p}):
        await file(repository, path + "/DEVELOPMENT.md", commit, "guide")
    # GitHub inherits missing community files from the owner's .github repo.
    missing_guide = not any("contributing" in s["path"].lower() for s in sources)
    missing_template = not any(s["role"] == "template" for s in sources)
    if missing_guide or missing_template:
        default_repo = repository.split("/")[0] + "/.github"
        info = await transport.api("GET", "repos/" + default_repo, missing=True)
        if info is not None:
            branch = info.get("default_branch") if isinstance(info, dict) else None
            if not isinstance(branch, str) or not branch:
                raise WorkbenchError("组织默认贡献规范无法确认", code="publication_requirements_invalid")
            revision = await transport.api("GET", f"repos/{default_repo}/commits/{quote(branch, safe='')}")
            ref = revision.get("sha", "")
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                raise WorkbenchError("组织规范提交无效", code="publication_requirements_invalid")
            for prefix in ("", ".github/", "docs/"):
                if missing_guide:
                    await file(default_repo, prefix + "CONTRIBUTING.md", ref, "guide")
                if missing_template:
                    for name in ("PULL_REQUEST_TEMPLATE.md", "pull_request_template.md"):
                        await file(default_repo, prefix + name, ref, "template")
            if missing_template:
                await directory(default_repo, ".github/PULL_REQUEST_TEMPLATE", ref, "template")
    checks = repository_checks(sources)
    result = {"version": VERSION, "repository": repository, "sources": sources,
              "title_rules": title_rules(sources),
              "body_rules": {"issue_first_line": any(c["code"] == "issue_first_line" for c in checks)},
              "repository_checks": checks,
              "manual_checks": ["Read and sign any required CLA yourself; generation cannot sign legal agreements.",
                                "Fork CI may require a maintainer to approve workflows. Local tests do not prove GitHub CI passed.",
                                "Review contribution guidelines and template checkboxes before confirming publication."]}
    result["hash"] = requirements_hash(result)
    return result


def requirements_hash(value: dict) -> str:
    # Keep commits in source provenance, but compare blob identities/content so
    # unrelated target movement does not invalidate identical conventions.
    return content_hash({**{k: v for k, v in value.items() if k not in {"hash", "sources"}},
        "sources": [{k: v for k, v in source.items() if k != "commit"} for source in value["sources"]]})


def title_rules(sources: list[dict]) -> dict:
    rules = {"conventional": False, "types": DEFAULT_TYPES, "scopes": [], "require_scope": False, "manual": []}
    for source in sources:
        text = source["content"]
        if source["role"] != "workflow" or "amannn/action-semantic-pull-request@" not in text:
            continue
        rules["conventional"] = True
        # Parse ONLY the literal with-block adjacent to this particular action.
        # Arbitrary YAML/expressions/regexes are never interpreted as programs.
        step = text.split("amannn/action-semantic-pull-request@", 1)[1]
        step = re.split(r"\n\s*-\s+(?:name|uses|run):", step, maxsplit=1)[0]
        with_match = re.search(r"(?m)^(\s+)with:\s*\n", step)
        block = ""
        if with_match:
            indent = len(with_match[1])
            for line in step[with_match.end():].splitlines():
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                block += line + "\n"
        for key in ("types", "scopes"):
            match = re.search(rf"(?m)^(\s+){key}:\s*\|\s*\n((?:[ \t]+[^\n]*\n?)*)", block)
            if match:
                indent = len(match[1])
                values = []
                for line in match[2].splitlines():
                    if line.strip() and len(line) - len(line.lstrip()) <= indent:
                        break
                    if line.strip():
                        values.append(line.strip())
                if values and all(re.fullmatch(r"[A-Za-z0-9_.*/-]{1,80}", x) and "*" not in x for x in values):
                    rules[key] = values
                else:
                    rules["manual"].append(source["path"] + ": " + key)
            elif re.search(rf"\b{key}:", block):
                rules["manual"].append(source["path"] + ": " + key)
        required = re.search(r"requireScope:\s*(true|false)\b", block)
        if required:
            rules["require_scope"] = required[1] == "true"
        if re.search(r"(?:subjectPattern|headerPattern|disallowScopes|subjectPatternError|wip):", block) or "${{" in block:
            rules["manual"].append(source["path"] + ": custom validation")
    return rules


def repository_checks(sources: list[dict]) -> list[dict]:
    """Recognize common explicit requirements; retain source references."""
    checks = []
    for source in sources:
        if source["role"] != "guide":
            continue
        text = re.sub(r"\s+", " ", source["content"]).lower()
        candidates = [
            ("issue_first_line", bool(re.search(r"first line of (?:the )?(?:pr )?description.{0,100}(?:reference|refer).{0,60}issue", text)),
             "The first line of the PR description must reference the original issue."),
            ("maintainer_confirmation", "explicit confirmation from a maintainer" in text or "maintainer approval before" in text,
             "Verify explicit maintainer confirmation before contributing; an open issue alone is not an invitation."),
            ("span_screenshot", "span" in text and bool(re.search(r"include a screenshot.{0,80}trace", text)),
             "If emitted spans change, include a screenshot showing the updated trace output."),
        ]
        for code, applies, description in candidates:
            if applies:
                checks.append({"code": code, "source": source["path"], "repository": source["repository"], "description": description})
    return checks


def validate_document(title: str, body: str, requirements: dict, template_path: str | None = None,
                      *, issue_number: int | None = None) -> None:
    ensure_no_sensitive_data({"title": title, "body": body}, context="publication document")
    if not title.strip() or len(title) > 200 or "\n" in title or "\r" in title or not body.strip() or len(body) > 7600:
        raise WorkbenchError("PR 标题或正文为空、过长或包含无效换行", code="publication_document_invalid")
    prose = re.sub(r"```[\s\S]*?```|`[^`]*`|https?://\S+", "", title + "\n" + body)
    if any(unicodedata.category(c).startswith("L") and "LATIN" not in unicodedata.name(c, "") for c in prose):
        raise WorkbenchError("PR 标题、正文和提交说明必须使用英文；请重新生成英文草稿", code="publication_document_language")
    rules = requirements["title_rules"]
    if requirements.get("body_rules", {}).get("issue_first_line"):
        first = body.strip().splitlines()[0]
        numbers = re.findall(r"(?:#|/issues/)([1-9][0-9]*)\b", first)
        if not numbers or (issue_number is not None and str(issue_number) not in numbers):
            raise WorkbenchError("仓库要求 PR 正文第一行引用本任务的原始 Issue", code="publication_document_issue")
    if rules["conventional"]:
        match = re.fullmatch(r"([a-z]+)(?:\(([A-Za-z0-9_./-]+)\))?(!)?: ([A-Za-z0-9][^\r\n]*)", title)
        if not match or match[1] not in rules["types"] or (rules["require_scope"] and not match[2]) or (match[2] and rules["scopes"] and match[2] not in rules["scopes"]):
            raise WorkbenchError("PR 标题不符合仓库的 Conventional Commits 类型或 scope 要求", code="publication_document_title")
    templates = [s for s in requirements["sources"] if s["role"] == "template"]
    if templates:
        selected = [s for s in templates if s["path"] == template_path]
        if not selected and len(templates) == 1:
            selected = templates
        if len(selected) != 1:
            raise WorkbenchError("请明确选择仓库要求的 PR 模板并重新生成草稿", code="publication_document_template")
        text = re.sub(r"<!--[\s\S]*?-->", "", selected[0]["content"])
        headings = re.findall(r"(?m)^#{1,6}\s+.+$", text)
        checkboxes = re.findall(r"(?m)^\s*[-*]\s+\[[ xX]\]\s+(.+)$", text)
        actual_headings = re.findall(r"(?m)^#{1,6}\s+.+$", body)
        if any(h not in actual_headings for h in headings) or any(label not in body for label in checkboxes):
            raise WorkbenchError("PR 正文缺少模板要求的标题或检查项", code="publication_document_template")
    if re.search(r"\b(?:TODO|TBD|PLACEHOLDER)\b|<describe[^>]*>", body, re.I):
        raise WorkbenchError("PR 正文仍有待填写的占位内容", code="publication_document_incomplete")
