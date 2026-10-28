"""Credential-free v2 tools. Every call reconstructs the same immutable view.

The logical workspace is an archive plus content-addressed file snapshots. A
lost patch acknowledgement is safe to replay against its original input; no
mutable host checkout or long-lived writable volume is authoritative.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from typing import Any, Callable
from pydantic import BaseModel

from app.agent_v2.contracts import Failure, TaskSpec, ToolCall, ToolResult
from app.archives import RepositoryArchiveStore
from app.provenance import canonical_json, content_hash
from app.sandbox_worker.implementation import (ChangeOperation, ChangeOperationKind,
    DockerImplementRuntime, ImplementationChangeSet)
from app.sandbox_worker.specs import SandboxCommand, SandboxPolicy
from app.sandbox_worker.verification import DockerVerifyRuntime, NormalizedTestResult
from app.execution_outcome import classify_verification
from app.security import ensure_no_sensitive_data


class DockerAgentToolGateway:
    def __init__(self, *, artifact_root: str, docker_environment: dict,
                 repository_for_attempt: Callable[[str], str],
                 dependency_for_attempt: Callable[[str], Any] | None = None) -> None:
        self.archives = RepositoryArchiveStore(artifact_root)
        self.materializer = DockerImplementRuntime(docker_environment=docker_environment)
        self.verifier = DockerVerifyRuntime(docker_environment=docker_environment)
        self.repository_for_attempt = repository_for_attempt
        self.dependency_for_attempt = dependency_for_attempt

    async def execute(self, spec: TaskSpec, call: ToolCall, arguments: BaseModel, *,
                      source_hash: str, snapshot: dict | None, input_hash: str) -> dict:
        from app.agent_v2.tools import RestrictedToolRegistry
        RestrictedToolRegistry().validate(call, spec)
        policy = SandboxPolicy()
        if policy.policy_hash != spec.binding.policy_hash:
            raise ValueError("agent_policy_mismatch")
        if snapshot and snapshot["source_hash"] != source_hash:
            raise ValueError("agent_snapshot_mismatch")
        repository = self.repository_for_attempt(spec.binding.attempt_id)
        archive = self.archives.lookup(repository_full_name=repository,
            base_commit_sha=spec.binding.base_sha, archive_hash=spec.binding.archive_hash)
        workspace = await self.materializer.create_workspace(image_digest=spec.binding.image, policy=policy)
        try:
            await self.materializer.materialize(workspace=workspace, archive=archive, policy=policy)
            files = dict(snapshot["files"]) if snapshot else {}
            if files:
                operations = tuple(ChangeOperation(path=p, kind=ChangeOperationKind.WRITE,
                    expected_prior_hash=v["original_hash"], content=v["content"], executable=v["executable"])
                    for p, v in sorted(files.items()))
                changes = ImplementationChangeSet(change_set_id="view-" + input_hash[:32],
                    plan_version_id=spec.binding.plan_id, plan_content_hash=spec.binding.plan_content_hash,
                    plan_record_hash=spec.binding.plan_hash, operations=operations)
                inspection = await self.materializer.apply(workspace=workspace, change_set=changes,
                    allowed_change_paths=tuple(spec.write_paths), policy=policy)
                if inspection.result_inventory_hash != source_hash:
                    raise ValueError("agent_reconstructed_view_mismatch")
            payload = {"name": call.name, "arguments": arguments.model_dump(),
                "write_paths": spec.write_paths,
                "originals": {p: v["original_content"] for p, v in files.items()}}
            raw = await self._source(workspace, policy, payload)
            if snapshot and raw["before_hash"] != source_hash:
                raise ValueError("agent_source_hash_mismatch")
            data = {"tool": call.name, "execution_backend": "docker", **raw.get("data", {})}
            output_hash = raw["after_hash"]
            reason = raw.get("error")
            new_snapshot = None
            if call.name == "apply_patch" and not reason:
                change = raw["file"]
                prior = files.get(change["path"])
                files[change["path"]] = {"original_hash": prior["original_hash"] if prior else change["original_hash"],
                    "original_content": prior["original_content"] if prior else change["original_content"],
                    "content": change["content"], "executable": change["executable"]}
                new_snapshot = {"version": "agent-workspace-snapshot-v1", "archive_hash": spec.binding.archive_hash,
                    "source_hash": output_hash, "files": files}
            if call.name == "run_approved_checks" and not reason:
                commands = tuple(SandboxCommand.from_wire(c) for c in spec.commands)
                dependency = self.dependency_for_attempt(spec.binding.attempt_id) if self.dependency_for_attempt else None
                actual = await self.verifier.run(workspace=replace(workspace, inventory_hash=output_hash),
                    commands=commands, policy=policy, dependency_workspace=dependency)
                if actual.before_inventory_hash != output_hash or actual.after_inventory_hash != output_hash:
                    raise ValueError("agent_verify_mutated_view")
                tests = [NormalizedTestResult.from_evidence(e).to_wire() for e in actual.command_evidence]
                verify = {"succeeded": actual.succeeded, "test_results": tests,
                    "test_results_hash": content_hash(tests),
                    "command_evidence": [e.to_wire() for e in actual.command_evidence]}
                outcome = classify_verification(verify)
                data.update(outcome=outcome["status"], verification=verify,
                    actual_checks_started=sum(e.started_at is not None for e in actual.command_evidence),
                    actual_tests_executed=None)
                if outcome["status"] == "environment_blocked":
                    reason = "agent_check_environment_blocked"
            ensure_no_sensitive_data(data, context="v2 tool result")
            info = None
            if reason:
                category = "environment" if reason == "agent_check_environment_blocked" else "protocol"
                if reason in {"path_denied", "symlink_denied"}:
                    category = "security"
                info = Failure(category=category, safe_reason_code=reason, retryable=category == "protocol",
                    remaining_budget={}, next_allowed_action="read_current_source" if category == "protocol" else "inspect_diagnostic",
                    blocking_scope="tool" if category == "protocol" else "session")
            result = ToolResult(call_id=call.call_id, input_hash=input_hash, source_hash=source_hash,
                output_source_hash=output_hash, status="rejected" if reason else "succeeded", data=data, failure=info)
            return {"result": result.model_dump(), "snapshot": new_snapshot}
        finally:
            await self.materializer.destroy(workspace)

    async def _source(self, workspace, policy, payload: dict) -> dict:
        # This mounts only a bounded Worker-created request, never a model path.
        with tempfile.TemporaryDirectory(prefix="arv2-tool-") as temporary:
            os.chmod(temporary, 0o755)
            request_path = Path(temporary) / "request.json"
            request_path.write_text(canonical_json(payload))
            request_path.chmod(0o444)
            name = "contribos-implement-apply-" + uuid4().hex
            argv = (*self.materializer._base_argv(workspace=workspace, policy=policy, container_name=name),
                "--mount", f"type=volume,src={workspace.volume_name},dst=/workspace" + ("" if payload["name"] == "apply_patch" else ",readonly"),
                "--mount", f"type=bind,src={request_path},dst=/input/request.json,readonly",
                "--entrypoint", "python3", workspace.runner_image_digest, "-I", "-c", SOURCE_TOOL_SCRIPT)
            raw = await self.materializer._container(argv, container_name=name, timeout_seconds=policy.timeout_seconds)
        result = json.loads(raw)
        ensure_no_sensitive_data(result, context="v2 sandbox source output")
        return result


SOURCE_TOOL_SCRIPT = r'''
import difflib, hashlib, json, os, stat
root = '/workspace'
request = json.load(open('/input/request.json'))
args, name = request['arguments'], request['name']

def digest(data): return hashlib.sha256(data).hexdigest()
def path_for(relative):
    if not isinstance(relative, str) or not relative or relative.startswith('/') or '\\' in relative or '\x00' in relative:
        raise ValueError('path_denied')
    parts = relative.split('/')
    if any(p in ('', '.', '..', '.git') for p in parts): raise ValueError('path_denied')
    current = root
    for part in parts:
        current = os.path.join(current, part)
        if os.path.islink(current): raise ValueError('symlink_denied')
    if os.path.commonpath((root, os.path.realpath(current))) != root: raise ValueError('path_denied')
    return current

def scan():
    entries=[]; total=0
    for parent, directories, files in os.walk(root, followlinks=False):
        directories.sort(); files.sort()
        for filename in list(directories):
            p=os.path.join(parent,filename)
            if os.path.islink(p):
                directories.remove(filename)
                entries.append({'path':os.path.relpath(p,root),'type':'symlink','target':os.readlink(p)})
        for filename in files:
            p=os.path.join(parent,filename); rel=os.path.relpath(p,root); st=os.lstat(p)
            if stat.S_ISLNK(st.st_mode): entries.append({'path':rel,'type':'symlink','target':os.readlink(p)}); continue
            if not stat.S_ISREG(st.st_mode): raise ValueError('special_file_denied')
            total+=st.st_size
            if total>128000000 or len(entries)>=20000: raise ValueError('source_limit')
            entries.append({'path':rel,'type':'file','size':st.st_size,'sha256':digest(open(p,'rb').read()),'executable':bool(st.st_mode&0o111)})
    entries.sort(key=lambda e:e['path'])
    return entries,digest(json.dumps(entries,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())

def page(data):
    offset=args.get('offset',0); size=args.get('max_bytes',4096)
    if offset>len(data): raise ValueError('page_offset_invalid')
    chunk=data[offset:offset+size]
    while chunk:
        try: text=chunk.decode('utf-8'); break
        except UnicodeDecodeError as e:
            if e.start==0: raise ValueError('page_offset_invalid')
            chunk=chunk[:e.start]
    if not chunk: text=''
    end=offset+len(chunk)
    return {'text':text,'offset':offset,'next_offset':end if end<len(data) else None,'total_bytes':len(data),'content_hash':digest(data)}

before, before_hash=scan(); output={'before_hash':before_hash,'after_hash':before_hash}
try:
    if name in ('list_paths','search_code'):
        rows=[]; prefix=args.get('prefix',''); query=args.get('query','')
        for e in before:
            if not e['path'].startswith(prefix): continue
            if name=='list_paths': rows.append(e['path']); continue
            if e['type']!='file' or e['size']>1000000: continue
            try: text=open(path_for(e['path']),'rb').read().decode('utf-8')
            except UnicodeDecodeError: continue
            byte_offset=0
            for line_no,line in enumerate(text.splitlines(keepends=True),1):
                if query in line:
                    rows.append(json.dumps({'path':e['path'],'line':line_no,'byte_offset':byte_offset,
                        'file_content_hash':e['sha256'],'text':line.rstrip('\r\n')[:1000]},ensure_ascii=False,sort_keys=True))
                byte_offset+=len(line.encode())
        output['data']=page(('\n'.join(rows)).encode())
    elif name=='read_file':
        p=path_for(args['path']); data=open(p,'rb').read(1000001)
        if len(data)>1000000: raise ValueError('source_file_limit')
        if args.get('expected_hash') and digest(data)!=args['expected_hash']: raise ValueError('source_hash_conflict')
        output['data']={'path':args['path'],**page(data)}
    elif name=='apply_patch':
        rel=args['path']
        if rel not in request['write_paths']: raise ValueError('path_denied')
        p=path_for(rel); exists=os.path.exists(p)
        old=open(p,'rb').read(1000001) if exists else b''
        if len(old)>1000000: raise ValueError('source_file_limit')
        prior=digest(old) if exists else None
        if prior!=args['expected_hash']: raise ValueError('patch_hash_conflict')
        text=old.decode('utf-8')
        if exists and text.count(args['old_text'])!=1: raise ValueError('patch_anchor_conflict')
        updated=text.replace(args['old_text'],args['new_text'],1) if exists else args['new_text']
        data=updated.encode()
        if data==old: raise ValueError('patch_no_change')
        if len(data)>1000000: raise ValueError('source_file_limit')
        executable=bool(os.stat(p).st_mode&0o111) if exists else False
        os.makedirs(os.path.dirname(p),exist_ok=True)
        temporary=p+'.arv2-new'
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o755 if executable else 0o644)
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temporary,p)
        after, output['after_hash']=scan()
        changed=[e['path'] for e in after if e not in before]
        if changed!=[rel]: raise ValueError('patch_inventory_conflict')
        output['file']={'path':rel,'original_hash':prior,'original_content':text,'content':updated,'executable':executable}
        output['data']={'path':rel,'content_hash':digest(data),'changed_paths':[rel]}
    elif name=='read_diff':
        pieces=[]
        for rel,old in sorted(request['originals'].items()):
            current=open(path_for(rel),'rb').read().decode('utf-8')
            pieces.extend(difflib.unified_diff(old.splitlines(True),current.splitlines(True),fromfile='a/'+rel,tofile='b/'+rel))
        output['data']=page(''.join(pieces).encode())
    elif name=='run_approved_checks': output['data']={}
    else: raise ValueError('tool_unavailable')
except (ValueError, FileNotFoundError, UnicodeDecodeError) as error:
    code=str(error) if isinstance(error,ValueError) and str(error) in {
        'path_denied','symlink_denied','source_limit','source_file_limit','source_hash_conflict',
        'page_offset_invalid','patch_hash_conflict','patch_anchor_conflict','patch_no_change','tool_unavailable'} else 'source_unavailable'
    output['error']=code
    if code in {'source_hash_conflict','patch_hash_conflict','patch_anchor_conflict'}:
        p=path_for(args['path']); current=open(p,'rb').read(1000001)
        if len(current)<=1000000:
            output['data']={'path':args['path'],'current_content_hash':digest(current),
                'preview':current[:512].decode('utf-8',errors='ignore'),'partial':len(current)>512,
                'next_allowed_action':'read_file_without_expected_hash'}
print(json.dumps(output,ensure_ascii=False,sort_keys=True,separators=(',',':')))
'''.strip()
