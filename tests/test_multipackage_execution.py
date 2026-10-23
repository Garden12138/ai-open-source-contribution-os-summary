"""Multi-package selection, signed recipes and source-correct verification."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tarfile
from dataclasses import replace

import pytest

from app.execution_environment import environment_plan
from app.execution_preflight import inspect_environment
from app.plans import PlanCommand
from app.provenance import content_hash
from app.sandbox_worker.dependencies import (
    DependencyManifest, DependencyPreparationPlan, DependencyPlanSigner,
    DependencyProxyPolicy, WORKSPACE_DEPENDENCY_PLAN_VERSION,
)
from app.sandbox_worker.python_sources import build_python_source_overlay
from app.sandbox_worker.specs import SandboxCommand, SandboxPolicy
from app.verification_commands import verification_command_error
from app.workspace_scope import selected_workspace_paths


def package_files():
    files = {}
    for name in ('alpha', 'beta'):
        root = 'python/' + name
        files[root + '/pyproject.toml'] = (
            '[build-system]\nrequires=["setuptools>=69", "wheel"]\nbuild-backend="setuptools.build_meta"\n'
            '[project]\nname="contribos-multi-' + name + '"\nversion="1.0.0"\n'
            '[tool.setuptools.packages.find]\nwhere=["src"]\n')
        files[root + '/src/shared_namespace/instrumentation/' + name + '.py'] = 'VALUE = 1\n'
        files[root + '/tests/test_' + name + '.py'] = (
            'from shared_namespace.instrumentation.' + name + ' import VALUE\n'
            'def test_value():\n    assert VALUE in (1, 2)\n')
    files['python/common/pyproject.toml'] = (
        '[build-system]\nrequires=["setuptools>=69", "wheel"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="common"\nversion="1.0.0"\n'
        '[tool.setuptools.packages.find]\nwhere=["src"]\n')
    files['python/common/src/shared_namespace/instrumentation/__init__.py'] = 'CORE = True\n'
    files['python/alpha/test-requirements.txt'] = 'packaging<24\n'
    files['python/beta/test-requirements.txt'] = 'common>=1.0.0\npackaging>=24\n'
    return files


def test_python_package_union_is_deterministic_bounded_and_preserves_single_selection():
    files = package_files()
    files['js/package.json'] = '{}'
    changes = ['python/beta/src/beta.py', 'python/alpha/src/alpha.py']
    selected = environment_plan(list(files), change_paths=changes)
    assert selected == environment_plan(list(reversed(files)), change_paths=list(reversed(changes)))
    assert selected['project_directories'] == ['python/alpha', 'python/beta']
    assert selected['project_directory'] == 'python/alpha'
    assert selected['manifests'] == ['python/alpha/pyproject.toml', 'python/alpha/test-requirements.txt', 'python/beta/pyproject.toml',
                                     'python/beta/test-requirements.txt']
    assert 'project_directories' not in environment_plan(list(files), change_paths=changes[:1])
    assert environment_plan(list(files), change_paths=[*changes, 'js/new.ts'])['status'] == 'unsupported'
    roots = ['pkg' + str(i) for i in range(5)]
    assert environment_plan([r + '/pyproject.toml' for r in roots],
                            change_paths=[r + '/new.py' for r in roots])['status'] == 'unsupported'
    scope = selected_workspace_paths({'status': 'supported', 'project_directory': 'a/pkg',
                                     'project_directories': ['a/pkg', 'b/pkg']},
                                    {'README.md', 'js/package.json'}, {'a/pkg/a.py', 'b/pkg/b.py'})
    assert scope == ('README.md', 'a', 'b')


@pytest.mark.parametrize('failure', ['none', 'missing', 'unavailable', 'tampered', 'url'])
def test_every_selected_manifest_is_read_and_validated_before_admission(failure):
    files = package_files()
    selected = environment_plan(list(files), change_paths=['python/alpha/a.py', 'python/beta/b.py'])
    entries = [{'path': p, 'content': files[p], 'prior_hash': hashlib.sha256(files[p].encode()).hexdigest()}
               for p in selected['manifests']]
    if failure == 'missing':
        entries = [e for e in entries if e['path'] != 'python/beta/pyproject.toml']
    elif failure == 'unavailable':
        entries[2]['content'] = None
    elif failure == 'tampered':
        entries[2]['prior_hash'] = '0' * 64
    elif failure == 'url':
        entries[3]['content'] = 'common @ https://untrusted.invalid/pkg\n'
        entries[3]['prior_hash'] = hashlib.sha256(entries[3]['content'].encode()).hexdigest()
    result = inspect_environment(selected, {'entries': entries})
    assert result['preparation_version'] == 'python-workspace-preparation-v3'
    assert result['status'] == ('supported' if failure == 'none' else 'pending_read' if failure == 'missing' else 'unsupported')
    if failure == 'missing':
        assert result['required_paths'] == ['python/beta/pyproject.toml']


def test_each_selected_package_needs_an_existing_test_baseline():
    files = package_files()
    changes = {'python/alpha/new.py', 'python/beta/new.py'}
    commands = [PlanCommand(name, name, ('python', '-m', 'pytest', 'tests'), 'python/' + name)
                for name in ('alpha', 'beta')]
    assert verification_command_error(commands, set(files), changes) is None
    assert verification_command_error(commands[:1], set(files), changes)['paths'] == ['python/beta']
    collected = [commands[0], replace(commands[1], argv=('python', '-m', 'pytest', '--collect-only', 'tests'))]
    assert verification_command_error(collected, set(files), changes)['constraint'] == 'package_baseline_missing'
    only_new = [commands[0], replace(commands[1], argv=('python', '-m', 'pytest', 'new.py'))]
    assert verification_command_error(only_new, set(files), changes)['constraint'] == 'package_baseline_missing'
    assert verification_command_error([*commands, replace(commands[0], working_directory='.')], set(files), changes)['constraint'] == 'package_command_unscoped'


def test_selected_roots_are_signed_without_changing_legacy_wires():
    from tests.test_sandbox_dependencies import _plan
    policy = SandboxPolicy(); proxy = DependencyProxyPolicy.for_ecosystem('python')
    old = _plan(policy, proxy)
    legacy = old.to_wire()
    plan = replace(old, version=WORKSPACE_DEPENDENCY_PLAN_VERSION, source_kind='explore',
                   project_directory='python/alpha', project_directories=('python/alpha', 'python/beta'),
                   preparation_version='python-workspace-preparation-v3',
                   manifests=(DependencyManifest('python/alpha/pyproject.toml', '1' * 64),
                              DependencyManifest('python/beta/pyproject.toml', '2' * 64)))
    signer = DependencyPlanSigner(key_id='multi', signing_key=b'm' * 32)
    assert signer.verify(signer.sign(plan)) == plan
    assert old.to_wire() == legacy and 'project_directories' not in legacy
    with pytest.raises(ValueError):
        replace(signer.sign(plan), plan=replace(plan, project_directories=('python/alpha', 'python/gamma'),
            manifests=(plan.manifests[0], DependencyManifest('python/gamma/pyproject.toml', '3' * 64))))
    for values in [(), ('python/beta', 'python/alpha'), ('python/alpha', 'python/alpha'),
                   ('../outside', 'python/beta')]:
        with pytest.raises(ValueError):
            replace(plan, project_directories=values)
    with pytest.raises(ValueError):
        replace(plan, preparation_version='python-workspace-preparation-v2')
    with pytest.raises(ValueError):
        replace(plan, manifests=plan.manifests[:1])
    with pytest.raises(ValueError):
        replace(plan, local_projects=('python/beta',))


def test_per_package_recipes_keep_conflicting_constraints_separate():
    import tomllib
    from app.sandbox_worker.package_environments import python_package_environments
    files = package_files()
    selected = environment_plan(list(files), change_paths=['python/alpha/new.py', 'python/beta/new.py'])
    plan = {**selected, 'local_projects': ['python/common'],
            'manifests': [{'path': p, 'content_hash': hashlib.sha256(text.encode()).hexdigest()}
                          for p, text in files.items() if p.endswith(('pyproject.toml', 'requirements.txt'))]}
    metadata = {name: tomllib.loads(files[name + '/pyproject.toml'])
                for name in ('python/alpha', 'python/beta', 'python/common')}
    alpha, beta = python_package_environments(plan, metadata, files)
    assert alpha['local_projects'] == []
    assert beta['local_projects'] == ['python/common']
    assert 'python/beta/test-requirements.txt' not in {m['path'] for m in alpha['manifests']}
    assert 'python/alpha/test-requirements.txt' not in {m['path'] for m in beta['manifests']}
    metadata['python/common']['project']['name'] = metadata['python/alpha']['project']['name']
    with pytest.raises(ValueError, match='Duplicate'):
        python_package_environments(plan, metadata, files)


def test_multi_package_approval_rejects_changed_recipe_before_network(tmp_path, monkeypatch):
    from app.contribution_workflow import authorize_execution, ContributionStartWorker
    from app.execution_preflight import task_preflight
    from app.planner import PlanningService
    from app.schemas import PlanVersionCreateRequest
    from tests.test_workbench import seeded
    db, settings, task, content, context = seeded(tmp_path)
    files = package_files()
    changes = ('python/alpha/src/shared_namespace/instrumentation/alpha.py',
               'python/beta/src/shared_namespace/instrumentation/beta.py')
    plan_content = replace(content, files_to_inspect=changes, files_likely_to_change=changes,
        commands_to_run=tuple(PlanCommand(name, name, ('python', '-m', 'pytest', 'tests'), 'python/' + name)
                              for name in ('alpha', 'beta')))
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        evidence = {'inventory': sorted(files), 'truncated': False,
                    'entries': [{'path': p, 'content': text, 'prior_hash': hashlib.sha256(text.encode()).hexdigest(),
                                 'executable': False} for p, text in files.items()]}
        context = wb.append(task, 'context_ready', {**context.payload, 'artifact_id': wb.artifact(evidence)}, key='multi-approval-context')
        plan = wb.save(task, PlanVersionCreateRequest.model_validate(plan_content.hash_payload()),
                       context_hash=context.record_hash, key='multi-approval-plan')
        session.commit()
        report = task_preflight(wb, task)
        assert report['status'] == 'ready'
        job = authorize_execution(wb, task, plan_id=plan.id, plan_hash=plan.record_hash, key='multi-approve', settings=settings)
        assert job.payload['preflight_hash'] == report['report_hash']
        assert job.payload['workspace_paths'] == ['python']
    monkeypatch.setattr('app.execution_preflight.MULTIPACKAGE_PREPARATION_VERSION', 'python-workspace-preparation-future')
    def no_network():
        pytest.fail('A changed multi-package recipe must reject before GitHub or repository execution')
    failed = asyncio.run(ContributionStartWorker(db, settings, no_network, worker_id='multi-stale').run_once())
    assert failed.state == 'failed' and '执行能力' in failed.error_message
    db.close()


def test_source_overlay_merges_regular_namespace_without_copying_source(tmp_path):
    files = package_files()
    workspace = tmp_path / 'workspace'
    for path, text in files.items():
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    roots = [str(workspace / ('python/' + name + '/src')) for name in ('alpha', 'beta', 'common')]
    overlay = tmp_path / 'overlay'
    build_python_source_overlay(roots, str(overlay), str(workspace))
    target = overlay / 'shared_namespace/instrumentation/beta.py'
    assert target.is_symlink() and target.read_text() == 'VALUE = 1\n'
    (workspace / 'python/beta/src/shared_namespace/instrumentation/beta.py').write_text('VALUE = 2\n')
    assert target.read_text() == 'VALUE = 2\n'
    assert (overlay / 'shared_namespace/instrumentation/__init__.py').is_symlink()
    # Paths escape, symlink input and conflicting modules cannot silently shadow.
    with pytest.raises(ValueError):
        build_python_source_overlay([str(tmp_path)], str(tmp_path / 'outside'), str(workspace))
    duplicate = workspace / 'python/beta/src/shared_namespace/instrumentation/alpha.py'
    duplicate.write_text('VALUE = 99\n')
    with pytest.raises(ValueError, match='conflicts'):
        build_python_source_overlay(roots, str(tmp_path / 'conflict'), str(workspace))
    duplicate.unlink()
    duplicate.symlink_to(workspace / 'python/alpha/src/shared_namespace/instrumentation/alpha.py')
    with pytest.raises(ValueError, match='inventory'):
        build_python_source_overlay(roots, str(tmp_path / 'symlink'), str(workspace))


@pytest.mark.parametrize('failure', [None, 'code', 'environment', 'legacy_code'])
def test_preparer_freezes_both_roots_and_test_only_local_dependency(tmp_path, monkeypatch, failure):
    from tests import test_workbench as fixtures
    from tests.test_sandbox_dependencies import _FakeDependencyRuntime, _lease
    from app.contribution_workflow import advance_workflows
    from app.models import CodingSession
    from app.planner import PlanningService
    from app.sandbox_worker.fake import FakeVerifyRuntime
    from app.sandbox_worker.verification import VerifyCommandStatus
    from sqlalchemy import select
    from app.workbench_view import workbench_view, execution_context

    message = ("ModuleNotFoundError: No module named 'missing_test_dependency'" if failure == 'environment'
               else 'src/example.py:17: error: missing library stubs [import-untyped]')
    if failure == 'legacy_code':
        from app.execution_outcome import classify_verification
        def old_summary(value):
            # Historical artifacts kept the full inspection but lost stdout in
            # their derived summary. Reading must repair the view, not history.
            result = classify_verification(value)
            for item in result['failures']:
                item.update(stdout='', output='')
            return result
        monkeypatch.setattr('app.sandbox_worker.workspace_jobs.classify_verification', old_summary)

    files = package_files()
    sources = tuple(p for p in files if p.endswith(('alpha.py', 'beta.py')) and '/src/' in p)
    base = fixtures._plan_content()
    monkeypatch.setattr(fixtures, '_plan_content', lambda: replace(base,
        files_to_inspect=(*sources, 'python/alpha/pyproject.toml', 'python/beta/pyproject.toml'),
        files_likely_to_change=sources,
        commands_to_run=tuple(PlanCommand(name, name, ('python', '-m', 'pytest', 'tests'), 'python/' + name)
                              for name in ('alpha', 'beta'))))
    entries = {p: {'path': p, 'content': text, 'prior_hash': hashlib.sha256(text.encode()).hexdigest(),
                   'executable': False} for p, text in files.items()}
    def configure(db, settings, task):
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            old = wb.latest(task, 'context_ready')
            evidence = wb.read(old.payload['artifact_id'])
            evidence.update(inventory=sorted(files), entries=list(entries.values()))
            wb.append(task, 'context_ready', {**old.payload, 'artifact_id': wb.artifact(evidence)}, key='multi-context')
    db, settings, task, workers, code, _ = fixtures.automatic_fixture(tmp_path, configure_models=configure)
    class Inspector:
        async def inspect(self, **kwargs):
            return {'entries': [entries[p] for p in kwargs['paths']]}
    class Proxy:
        async def start(self, *, image_digest, policy): return _lease(policy)
        async def stop(self, lease, **kwargs): pass
    class Runtime(_FakeDependencyRuntime):
        async def prepare(self, **kwargs):
            self.plan = kwargs['signed_plan'].plan
            return await super().prepare(**kwargs)
    class Verifier(FakeVerifyRuntime):
        async def run(self, **kwargs):
            assert [c.working_directory for c in kwargs['commands']] == ['python/alpha', 'python/beta']
            value = await super().run(**kwargs)
            if failure:
                value = replace(value, command_evidence=(value.command_evidence[0], replace(value.command_evidence[1],
                    status=VerifyCommandStatus.FAILED, exit_code=1, stdout_log=message,
                    stdout_bytes=len(message), stdout_hash=hashlib.sha256(message.encode()).hexdigest())))
            return value
    runtime = Runtime()
    workers[-1].dependencies, workers[-1].proxy, workers[-1].inspector = runtime, Proxy(), Inspector()
    workers[-1].verifier = Verifier()
    assert asyncio.run(workers[0].run_once()).state == 'succeeded'
    advance_workflows(db, settings)
    job = asyncio.run(workers[-1].run_once())
    assert job.state == 'succeeded', job.error_message
    assert runtime.plan.project_directories == ('python/alpha', 'python/beta')
    assert runtime.plan.local_projects == ('python/common',)
    assert runtime.plan.preparation_version == 'python-workspace-preparation-v3'
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        baseline = wb.read(wb.latest(task, 'execution_baseline_ready').payload['result_hash'])
        assert len(baseline['inspection']['command_evidence']) == 2
        status = 'environment_blocked' if failure == 'environment' else 'failed' if failure else 'passed'
        assert baseline['outcome']['status'] == status
        assert wb.read(baseline['environment_spec_hash']) == runtime.plan.to_wire()
        assert session.scalar(select(CodingSession)) is None
        if failure:
            assert baseline['outcome']['failures'][0]['stdout'] == ('' if failure == 'legacy_code' else message)
            history = [(e.id, e.record_hash) for e in wb.history(task)]
            for view in (workbench_view(wb, task), execution_context(wb, task)):
                assert view['baseline']['outcome']['status'] == status
                assert view['baseline']['outcome']['failures'][0]['stdout'] == message
                assert view['verification']['status'] == 'not_run'
            assert [(e.id, e.record_hash) for e in wb.history(task)] == history
    if failure:
        advance_workflows(db, settings)
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            assert wb.active_blocker(task) is None
            assert any(j.kind == 'baseline_diagnosis' and j.state == 'queued' for j in wb.jobs(task))
            assert session.scalar(select(CodingSession)) is None
        assert not code.invocations
    db.close()


@pytest.mark.skipif(os.getenv('CONTRIBOS_RUN_DOCKER_ACCEPTANCE') != '1', reason='real multi-package sandbox acceptance')
@pytest.mark.parametrize('conflict', [False, True])
def test_real_shared_namespace_uses_modified_source_from_both_selected_packages(tmp_path, conflict):
    from app.sandbox_worker.dependencies import DockerDependencyRuntime, DockerDependencyProxyRuntime
    from app.sandbox_worker.explore import RepositoryArchive
    from app.sandbox_worker.implementation import DockerImplementRuntime, ImplementationChangeSet, ChangeOperation, ChangeOperationKind
    from app.sandbox_worker.verification import DockerVerifyRuntime

    files = package_files()
    if conflict:
        files['python/beta/test-requirements.txt'] = 'common>=2.0.0\n'
    path = tmp_path / 'repository.tar.gz'
    with tarfile.open(path, 'w:gz') as bundle:
        for name, text in files.items():
            item = tarfile.TarInfo('fixture-' + 'a' * 40 + '/' + name)
            item.size, item.mode = len(text.encode()), 0o644
            bundle.addfile(item, io.BytesIO(text.encode()))
    archive = RepositoryArchive(repository_full_name='fixture/fixture', base_commit_sha='a' * 40,
        archive_hash=hashlib.sha256(path.read_bytes()).hexdigest(), path=path, size_bytes=path.stat().st_size)
    inventory = content_hash([{'path': p, 'type': 'file', 'size': len(text.encode()),
        'sha256': hashlib.sha256(text.encode()).hexdigest(), 'executable': False} for p, text in sorted(files.items())])
    image = os.environ['CONTRIBOS_SANDBOX_RUNNER_IMAGE']
    environment = {k: v for k, v in os.environ.items() if k in {'HOME', 'PATH', 'TMPDIR', 'DOCKER_HOST',
        'DOCKER_CONFIG', 'DOCKER_CONTEXT', 'DOCKER_CERT_PATH', 'DOCKER_TLS_VERIFY'}}
    policy = SandboxPolicy(); proxy_policy = DependencyProxyPolicy.for_ecosystem('python')
    signer = DependencyPlanSigner(key_id='multipackage', signing_key=b'p' * 32)
    plan = DependencyPreparationPlan(plan_id='multipackage', execution_attempt_id='fixture',
        implement_result_hash='1' * 64, workspace_inventory_hash=inventory, ecosystem='python',
        manifests=tuple(DependencyManifest(p, hashlib.sha256(text.encode()).hexdigest())
            for p, text in sorted(files.items()) if p.endswith(('pyproject.toml', 'requirements.txt'))),
        runner_image_digest=image, sandbox_policy_hash=policy.policy_hash, proxy_policy_hash=proxy_policy.policy_hash,
        disk_bytes=policy.disk_bytes, version=WORKSPACE_DEPENDENCY_PLAN_VERSION, source_kind='explore',
        project_directory='python/alpha', project_directories=('python/alpha', 'python/beta'),
        local_projects=('python/common',), preparation_version='python-workspace-preparation-v3')
    materializer = DockerImplementRuntime(docker_environment=environment)
    dependencies = DockerDependencyRuntime(docker_environment=environment)
    proxy = DockerDependencyProxyRuntime(docker_environment=environment)
    verifier = DockerVerifyRuntime(docker_environment=environment)
    async def run():
        workspace = await materializer.create_workspace(image_digest=image, policy=policy)
        dependency = lease = None
        try:
            await materializer.materialize(workspace=workspace, archive=archive, policy=policy)
            lease = await proxy.start(image_digest=image, policy=proxy_policy)
            if conflict:
                from app.sandbox_worker.dependencies import DependencyPreparationError
                with pytest.raises(DependencyPreparationError) as failure:
                    await dependencies.prepare(signed_plan=signer.sign(plan), signer=signer,
                        repository_volume=workspace.volume_name, proxy_policy=proxy_policy, proxy_lease=lease, sandbox_policy=policy)
                assert failure.value.diagnostic['reason_code'] == 'dependency_conflict'
                return
            dependency = await dependencies.prepare(signed_plan=signer.sign(plan), signer=signer,
                repository_volume=workspace.volume_name, proxy_policy=proxy_policy, proxy_lease=lease, sandbox_policy=policy)
            await proxy.stop(lease, ignore_missing=True); lease = None
            commands = tuple(SandboxCommand(command_id=name, argv=('python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests'),
                                           working_directory='python/' + name) for name in ('alpha', 'beta'))
            before = await verifier.run(workspace=replace(workspace, inventory_hash=inventory), commands=commands,
                                        policy=policy, dependency_workspace=dependency.workspace)
            assert all(e.exit_code == 0 for e in before.command_evidence), [(e.stdout_log, e.stderr_log) for e in before.command_evidence]
            operations = tuple(ChangeOperation(path=p, kind=ChangeOperationKind.WRITE,
                expected_prior_hash=hashlib.sha256(text.encode()).hexdigest(), content='VALUE = 2\n', executable=False)
                for p, text in files.items() if '/src/' in p and p.endswith(('alpha.py', 'beta.py')))
            changes = ImplementationChangeSet(change_set_id='both-packages', plan_version_id='fixture-plan',
                plan_content_hash='2' * 64, plan_record_hash='3' * 64, operations=operations)
            modified = await materializer.apply(workspace=workspace, change_set=changes,
                allowed_change_paths=tuple(o.path for o in operations), policy=policy)
            checks = tuple(SandboxCommand(command_id='modified_' + name, argv=('python', '-c',
                "import os,json,packaging,sys; from shared_namespace.instrumentation import " + name + " as module; "
                "assert module.VALUE==2; assert 'contribos-python-sources-' in module.__file__; "
                "assert sys.prefix=='/dependencies/output/environments/" + str(index) + "/venv'; "
                "assert int(packaging.__version__.split('.')[0]) " + ('<24' if name == 'alpha' else '>=24') + "; "
                "assert os.statvfs('/dependencies').f_flag & os.ST_RDONLY; assert len(os.listdir('/sys/class/net'))==1; "
                "r=json.load(open('/dependencies/output/package-environments.json')); assert len(r['packages'])==2"),
                working_directory='python/' + name) for index, name in enumerate(('alpha', 'beta')))
            after = await verifier.run(workspace=replace(workspace, inventory_hash=modified.result_inventory_hash),
                commands=(*commands, *checks), policy=policy, dependency_workspace=dependency.workspace)
            assert all(e.exit_code == 0 for e in after.command_evidence), [(e.stdout_log, e.stderr_log) for e in after.command_evidence]
            assert after.before_inventory_hash == after.after_inventory_hash == modified.result_inventory_hash
        finally:
            if lease: await proxy.stop(lease, ignore_missing=True)
            if dependency: await dependencies.destroy(dependency.workspace, ignore_missing=True)
            await materializer.destroy(workspace)
    asyncio.run(run())
