"""Offline overlay from the currently deployed immutable local image IDs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from preflight import ROOT, SOURCE, CODE, OLD_IMAGES, RUNNER, docker, local, save


def main() -> None:
    assert not (ROOT / 'build-report.json').exists()
    gate = json.loads((ROOT / 'preflight.json').read_text())
    assert gate['ready_for_controlled_local_deployment'] and gate['expected_migrations'] == 0
    local()
    assert not any(p.is_symlink() for p in Path('app').rglob('*'))
    images, verification = {}, {}
    with tempfile.TemporaryDirectory(prefix='coding-recovery-v11-build-') as temporary:
        context = Path(temporary)
        shutil.copytree('app',context / 'app',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for role, base in OLD_IMAGES.items():
            old = docker('image','inspect',base)[0]; assert old['Id'] == base and old['Config']['WorkingDir'] == '/app'
            alias = 'coding-recovery-v11-base-' + role + ':' + base[7:19]
            subprocess.run(['docker','tag',base,alias],check=True)
            assert docker('image','inspect',alias)[0]['Id'] == base
            (context / 'Dockerfile').write_text('FROM ' + alias + '\n'
                + 'LABEL contribos.source_commit="' + CODE + '" contribos.source_hash="' + SOURCE + '"\n'
                + 'COPY --chown=501:20 app /app/app\n')
            image = subprocess.check_output(['docker','build','--network=none','--pull=false','--quiet','-t',
                'coding-recovery-v11-' + role + ':' + SOURCE[:12],str(context)],
                env={**os.environ,'DOCKER_BUILDKIT':'0'},text=True).strip().splitlines()[-1]
            assert image.startswith('sha256:')
            info = docker('image','inspect',image)[0]
            assert info['RootFS']['Layers'][:-1] == old['RootFS']['Layers']
            for field in ('User','Env','Entrypoint','Cmd','WorkingDir','Volumes'):
                assert info['Config'].get(field) == old['Config'].get(field), field
            assert docker('image','inspect',alias)[0]['Id'] == base
            code = '''import hashlib,json
from pathlib import Path
from app.provenance import content_hash
from app.agent_v2.native_tools import native_model_transport,NATIVE_PROMPT_VERSION
from app.runtime_integrity import missing_runtime_modules
f={str(p.relative_to('/app')):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/app/app').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
assert not missing_runtime_modules(Path('/app/app'))
print(json.dumps({'files':len(f),'source_hash':content_hash(f),'transport':native_model_transport().model_dump(),'prompt':NATIVE_PROMPT_VERSION}))'''
            probe = json.loads(subprocess.check_output(['docker','run','--rm','--network','none','--read-only',
                '--user','501:20','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--memory','512m','--cpus','1','--pids-limit','128','--entrypoint','python',image,'-B','-c',code]))
            assert probe['files'] == 255 and probe['source_hash'] == SOURCE
            assert probe['transport'] == gate['local']['native_transport'] and probe['prompt'] == gate['local']['prompt_version']
            images[role] = image
            verification[role] = {**probe,'base_layers_retained':True,'one_source_copy_layer':True,
                'image_configuration_and_dependencies_unchanged':True,'architecture':info['Architecture'],'os':info['Os'],
                'image_id_is_local_config_digest':True,'registry_repo_digests':info.get('RepoDigests',[])}
    assert docker('image','inspect',RUNNER)[0]['Id'] == RUNNER
    result = save('build-report.json',{'source_commit':CODE,'source_hash':SOURCE,'files_verified':255,
        'images':images,'base_images':OLD_IMAGES,'runner_unchanged':RUNNER,'verification':verification,
        'build_network':'none','pull':False,'dependency_installations':0,'new_migrations':0,
        'remote_registry_writes':0,'paid_model_calls':0})
    print(json.dumps({'build':'passed','source_hash':SOURCE,'images':images,'runner_unchanged':RUNNER}))


if __name__ == '__main__': main()
