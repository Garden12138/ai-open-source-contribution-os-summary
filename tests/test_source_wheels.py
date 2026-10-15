"""Source builds are a new signed capability and stay offline with narrow mounts."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.sandbox_worker.source_wheels import validate_source_build_request
from app.sandbox_worker.dependencies import DependencyPlanSigner, DependencyWorkspace, DockerDependencyRuntime
from app.sandbox_worker.specs import SandboxPolicy
from tests.test_sandbox_dependencies import _plan,_lease,DependencyProxyPolicy


def request():
    return {'version':'source-wheel-request-v1','package':'sample','package_version':'1.0',
        'source_hash':'a'*64,'build_root':'source-build-inputs/sample',
        'source_directory':'source-build-inputs/sample/source/sample-1.0','python_version':[3,11]}


@pytest.mark.parametrize('changes',[
    {'source_directory':'/etc/passwd'}, {'source_directory':'source-build-inputs/sample/source/../../escape'},
    {'source_directory':'source-build-inputs/other/source/sample-1.0'}, {'build_root':'../host'},
    {'package':'sample @ https://untrusted.invalid/'}, {'source_hash':'forged'},
    {'raw_log':'untrusted'}, {'python_version':[2,7]},
])
def test_source_requests_reject_untrusted_paths_and_unknown_fields(changes):
    with pytest.raises(ValueError): validate_source_build_request({**request(),**changes})


def test_signed_source_recipe_does_not_reinterpret_legacy_approvals(tmp_path):
    from app.sandbox_worker.dependencies import DependencyManifest
    policy=SandboxPolicy(); proxy=DependencyProxyPolicy.for_ecosystem('python')
    legacy=replace(_plan(policy,proxy),version='workspace-dependency-plan-v3',source_kind='explore',
        manifests=(DependencyManifest('pyproject.toml','3'*64),),metadata_strategy='wheel-metadata-v1')
    source=replace(legacy,version='workspace-dependency-plan-v4',distribution_strategy='source-wheel-v1',builder_image_digest='sha256:'+'b'*64)
    signer=DependencyPlanSigner(key_id='source',signing_key=b's'*32)
    assert signer.verify(signer.sign(source))==source
    assert source.plan_hash!=legacy.plan_hash and 'builder_image_digest' not in legacy.to_wire()
    with pytest.raises(ValueError,match='wheel-only'): replace(source,version='workspace-dependency-plan-v3')
    changed=replace(source,builder_image_digest='sha256:'+'c'*64)
    assert changed.plan_hash!=source.plan_hash
    signed=signer.sign(source)
    with pytest.raises(ValueError): replace(signed,plan=changed)
    workspace=DependencyWorkspace(workspace_id='dependencies:fixture',volume_name='contribos-dependencies-'+'9'*32,
        runner_image_digest=source.runner_image_digest,sandbox_policy_hash=policy.policy_hash,plan_hash=source.plan_hash)
    runtime=DockerDependencyRuntime(docker_environment={})
    path=tmp_path/'plan.json'; path.write_bytes(signer.sign(source).to_bytes())
    online=runtime.build_prepare_argv(repository_volume='contribos-workspace-'+'a'*32,dependency_workspace=workspace,
        signed_plan_path=str(path),proxy_lease=_lease(proxy),policy=policy,container_name='contribos-dependency-worker-'+'1'*32)
    offline=runtime.build_source_argv(online,workspace=workspace,plan=source,request=request(),container_name='contribos-dependency-worker-'+'2'*32)
    assert offline[offline.index('--network')+1]=='none'
    assert source.builder_image_digest in offline
    mounts=[offline[i+1] for i,v in enumerate(offline) if v=='--mount']
    assert len(mounts)==1
    assert any('dst=/source-inputs,readonly,volume-subpath=' in m for m in mounts)
    assert not any('dst=/workspace' in m or 'type=bind' in m for m in mounts)
    env=[offline[i+1] for i,v in enumerate(offline) if v=='--env']
    assert not any(v.startswith(('HTTP_PROXY=','HTTPS_PROXY=','NO_PROXY=')) for v in env)
    assert offline[offline.index('--memory')+1]==str(policy.memory_bytes)
    assert offline[offline.index('--user')+1]=='65532:65532'
    assert offline[offline.index('--cap-drop')+1]=='ALL'
    assert json.loads(offline[-1])==request()
    with pytest.raises(ValueError,match='authorized'):
        runtime.build_source_argv(online,workspace=workspace,plan=legacy,request=request(),container_name='contribos-dependency-worker-'+'2'*32)


@pytest.mark.parametrize('enabled',[False,True])
def test_source_request_requires_new_strategy_and_cleans_up(enabled):
    import asyncio
    from tests.test_sandbox_dependencies import _FakeDependencyRuntime
    from app.sandbox_worker.dependencies import DependencyManifest,SourceBuildRequired,DependencyPreparationError
    policy=SandboxPolicy(); proxy=DependencyProxyPolicy.for_ecosystem('python')
    plan=replace(_plan(policy,proxy),version='workspace-dependency-plan-v3',source_kind='explore',
        manifests=(DependencyManifest('pyproject.toml','3'*64),),metadata_strategy='wheel-metadata-v1')
    if enabled:
        plan=replace(plan,version='workspace-dependency-plan-v4',distribution_strategy='source-wheel-v1',builder_image_digest='sha256:'+'b'*64)
    class Runtime(_FakeDependencyRuntime):
        async def _run(self,argv,**kwargs):
            self.prepares.append(tuple(argv))
            if len(self.prepares)==1: raise SourceBuildRequired(request())
            if len(self.prepares)==2:
                assert argv[argv.index('--network')+1]=='none'
                return json.dumps({'receipt':{'request':request()},'wheel_data':'ZmFrZQ=='}).encode()
            if len(self.prepares)==3:
                assert argv[argv.index('--network')+1]=='none'
                return b'{"version":"source-wheel-built-v1","package":"sample"}'
            return b'{"file_count":3,"inventory_hash":"'+b'e'*64+b'","total_bytes":1024}'
    runtime=Runtime(); signer=DependencyPlanSigner(key_id='fixture',signing_key=b'f'*32)
    async def run():
        return await runtime.prepare(signed_plan=signer.sign(plan),signer=signer,repository_volume='contribos-workspace-'+'a'*32,
            proxy_policy=proxy,proxy_lease=_lease(proxy),sandbox_policy=policy)
    if enabled:
        result=asyncio.run(run())
        assert len(runtime.prepares)==4 and result.result.plan_hash==plan.plan_hash
        asyncio.run(runtime.destroy(result.workspace))
    else:
        with pytest.raises(DependencyPreparationError,match='authorized'): asyncio.run(run())
        assert len(runtime.prepares)==1
    assert any(command[1:3]==('volume','rm') for command in runtime.commands)


@pytest.mark.parametrize('kind',['parent','absolute','symlink','hardlink','duplicate','size','count'])
def test_source_archive_boundary_rejects_escapes_and_exhaustion(tmp_path,kind):
    import io,tarfile
    from app.sandbox_worker.source_wheels import extract_source_archive
    archive=tmp_path/'source.tar.gz'
    with tarfile.open(archive,'w:gz') as bundle:
        name={'parent':'../escape','absolute':'/tmp/escape'}.get(kind,'package/file')
        member=tarfile.TarInfo(name)
        if kind in {'symlink','hardlink'}:
            member.type=tarfile.SYMTYPE if kind=='symlink' else tarfile.LNKTYPE
            member.linkname='../../escape'
            bundle.addfile(member)
        else:
            member.size=4; bundle.addfile(member,io.BytesIO(b'data'))
            if kind in {'duplicate','count'}:
                second=tarfile.TarInfo(name if kind=='duplicate' else 'package/other')
                second.size=4; bundle.addfile(second,io.BytesIO(b'data'))
    destination=tmp_path/'extracted'; destination.mkdir()
    with pytest.raises((ValueError,OSError)):
        extract_source_archive(str(archive),str(destination),max_bytes=2 if kind=='size' else 100,
                               max_entries=1 if kind=='count' else 10)
    assert not (tmp_path/'escape').exists()
