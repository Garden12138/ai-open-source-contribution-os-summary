"""Network-free application overlay build; runner/dependencies remain unchanged."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from app.provenance import content_hash

ROOT=Path(__file__).resolve().parent
manifest=json.loads((ROOT/'source-manifest.json').read_text())
assert content_hash(manifest['files'])==manifest['source_hash']
assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in manifest['files'].items())
bases={'app':'sha256:d92bfb14dd645a3f0bf05a33f9910becebb2c7d1febb5f9f85bed6b53dfb588c',
 'worker':'sha256:1d5c9749654e8de53ca876762c56ea015f27aac31bf63def9a8b64333b5fa833'}
images={}
with tempfile.TemporaryDirectory(prefix='harness-capacity-build-') as temp:
 root=Path(temp);shutil.copytree('app',root/'app',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 for role,base in bases.items():
  workdir=subprocess.check_output(['docker','image','inspect','--format','{{.Config.WorkingDir}}',base],text=True).strip()
  assert workdir=='/app'
  local_base='harness-capacity-base-'+role+':frozen'
  subprocess.run(['docker','tag',base,local_base],check=True)
  (root/'Dockerfile').write_text('FROM '+local_base+'\nCOPY --chown=501:20 app /app/app\n')
  images[role]=subprocess.check_output(['docker','build','--network=none','--pull=false','--quiet','-t',
    'harness-capacity-'+role+':'+manifest['source_hash'][:12],str(root)],text=True,env={**os.environ,"DOCKER_BUILDKIT":"0"}).strip().splitlines()[-1]
  assert images[role].startswith('sha256:')
report={'version':'harness-build-v1','source_hash':manifest['source_hash'], 'source_manifest_file':str(ROOT.relative_to(Path.cwd()))+'/source-manifest.json',
 'files_verified':len(manifest['files']),'images':images,'base_images':bases,'build_network':'none','dependencies_changed':False,'model_calls':0,'third_party_writes':0}
with (ROOT/'build-report.json').open('x') as out:out.write(json.dumps(report,indent=2)+'\n')
print(json.dumps({'source_hash':manifest['source_hash'],'images':images}))
