"""Actual credential-free OCI preflight and read batches; no model invocation."""
import asyncio
import hashlib
import io
import os
import tarfile

import pytest

from app.agent_v2.contracts import ToolCall
from app.agent_v2.tools import RestrictedToolRegistry
from app.archives import RepositoryArchiveStore
from app.provenance import content_hash
from app.sandbox_worker.agent_tools import DockerAgentToolGateway
from tests.test_agent_v2_tools import tool_spec

pytestmark = pytest.mark.skipif(os.getenv('CONTRIBOS_RUN_DOCKER_ACCEPTANCE') != '1',
    reason='Requires actual OCI image and engine; never a fake pass')


def actual(tmp_path):
    source=('line with UTF-8 中文\n'*600).encode()
    archive=tmp_path/'batch-source.tar.gz'
    with tarfile.open(archive,'w:gz') as bundle:
        entry=tarfile.TarInfo('fixture-'+'e'*40+'/calc.py');entry.size=len(source);entry.mode=0o644
        bundle.addfile(entry,io.BytesIO(source))
        link=tarfile.TarInfo('fixture-'+'e'*40+'/linked');link.type=tarfile.SYMTYPE;link.linkname='calc.py';bundle.addfile(link)
    root=str(tmp_path/'artifacts');digest=RepositoryArchiveStore(root).put_file(archive)
    spec=tool_spec(digest,os.environ['CONTRIBOS_SANDBOX_RUNNER_IMAGE'])
    env={k:os.environ[k] for k in ('HOME','PATH','TMPDIR','DOCKER_CONTEXT','DOCKER_HOST') if k in os.environ}
    gateway=DockerAgentToolGateway(artifact_root=root,docker_environment=env,repository_for_attempt=lambda _:'fixture/fixture')
    calls=[ToolCall(call_id='original-provider-'+str(i),name='read_file',arguments={'path':'calc.py',
        'offset':i*len('line with UTF-8 中文\n'.encode()),'max_bytes':128}) for i in range(8)]
    return spec,gateway,calls,source


@pytest.mark.parametrize('count',[5,6,8])
def test_actual_read_only_oci_preflight_then_four_call_children(tmp_path,count):
    spec,gateway,calls,source=actual(tmp_path);calls=calls[:count]
    async def scenario():
        proof=await gateway.preflight_read_batch(spec,calls,source_hash=spec.binding.archive_hash,snapshot=None,input_hash='a'*64)
        assert proof['status']=='succeeded' and proof['source_hash']==proof['after_hash']
        assert len(proof['checked'])==count and proof['execution_backend']=='docker'
        assert all(c['content_hash']==hashlib.sha256(source).hexdigest() for c in proof['checked'])
        results=[]
        for start in range(0,count,4):
            assert len(calls[start:start+4])<=4
            for c in calls[start:start+4]:
                result=(await gateway.execute(spec,c,RestrictedToolRegistry().validate(c,spec),
                    source_hash=proof['source_hash'],snapshot=None,input_hash=content_hash(c.model_dump())))['result']
                assert result['status']=='succeeded' and result['output_source_hash']==proof['source_hash']
                d=result['data'];assert source[d['offset']:d['offset']+len(d['text'].encode())]==d['text'].encode()
                assert d['next_offset']==d['offset']+len(d['text'].encode())
                results.append(result)
        assert [r['call_id'] for r in results]==[c.call_id for c in calls]
    asyncio.run(scenario())


@pytest.mark.parametrize('bad',['symlink','missing','stale_hash','bad_offset'])
def test_actual_last_read_preflight_failure_rejects_every_application_read(tmp_path,bad):
    spec,gateway,calls,_=actual(tmp_path);calls=calls[:6]
    arguments={'path':'linked' if bad=='symlink' else 'missing.py' if bad=='missing' else 'calc.py'}
    if bad=='stale_hash':arguments['expected_hash']='0'*64
    if bad=='bad_offset':arguments['offset']=999999
    calls[-1]=calls[-1].model_copy(update={'arguments':arguments})
    async def scenario():
        proof=await gateway.preflight_read_batch(spec,calls,source_hash=spec.binding.archive_hash,snapshot=None,input_hash='a'*64)
        assert proof['status']=='rejected' and proof['checked']==[]
        assert proof['source_hash']==proof['after_hash']
        assert proof['failure_code'] == {'symlink':'symlink_denied','missing':'source_not_found',
            'stale_hash':'source_hash_conflict','bad_offset':'page_offset_invalid'}[bad]
        assert 'text' not in proof and 'preview' not in proof
    asyncio.run(scenario())
