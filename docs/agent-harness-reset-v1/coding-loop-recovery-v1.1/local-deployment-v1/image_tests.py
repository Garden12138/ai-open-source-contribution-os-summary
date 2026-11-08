"""Run deterministic tests against the exact deployed app image, offline.

Only cached pure-Python pytest tooling and tracked test/doc fixtures are mounted
read-only. No production volume, Docker socket, credentials or host HOME enters.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from preflight import ROOT, HEAD, SOURCE, save, docker

FOCUS = ['tests/test_coding_recovery_v11.py','tests/test_coding_stop_boundaries.py',
    'tests/test_coding_working_set_boundaries.py','tests/test_coding_loop_recovery.py','tests/test_coding_delivery_boundaries.py']
RECOVERY = ['tests/test_native_read_batch_boundaries.py','tests/test_native_protocol_repairs.py',
    'tests/test_harness_coding_journey.py','tests/test_agent_v2_loop.py','tests/test_execution_scheduling_recovery.py']


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument('group',choices=('focus','recovery','replay')); args = parser.parse_args()
    build = json.loads((ROOT / 'build-report.json').read_text()); image = build['images']['app']
    assert (ROOT / 'deployment-report.json').exists()
    assert not (ROOT / ('image-tests-' + args.group + '.json')).exists()
    with tempfile.TemporaryDirectory(prefix='coding-v11-image-tests-') as temporary:
        base = Path(temporary); work = base / 'work'; work.mkdir(); tools = base / 'tools'; tools.mkdir()
        output = base / 'evidence'; output.mkdir()
        raw = subprocess.check_output(['git','archive','--format=tar',HEAD,'tests','docs','pyproject.toml'])
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive: archive.extractall(work,filter='data')
        versions = {}
        for name, distribution in [('pytest','pytest'),('_pytest','pytest'),('pluggy','pluggy'),
                ('iniconfig','iniconfig'),('packaging','packaging'),('pygments','pygments')]:
            origin = Path(importlib.util.find_spec(name).origin).parent
            shutil.copytree(origin,tools / name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            assert not any(p.suffix in {'.so','.dylib'} for p in (tools / name).rglob('*'))
            dist = importlib.metadata.distribution(distribution)
            metadata_path = tools / (distribution + '-' + dist.version + '.dist-info')
            metadata_path.mkdir(exist_ok=True)
            (metadata_path / 'METADATA').write_text(dist.read_text('METADATA'))
            versions[distribution] = dist.version
        # pytest's distribution also ships a top-level compatibility module;
        # it is not a separate dependency and is needed by _pytest.compat.
        shim = Path(importlib.util.find_spec('py').origin)
        assert shim.name == 'py.py'
        shutil.copy2(shim,tools / 'py.py')
        # Crash fixtures deliberately pass only PATH/LANG to child Python.
        # Give their cwd the ordinary repository/test-tool layout, resolving
        # app to this image's exact source, never the old installed package.
        (work / 'app').symlink_to('/app/app',target_is_directory=True)
        (work / 'data').symlink_to('/tmp/import-data',target_is_directory=True)
        for package in tools.iterdir():
            (work / package.name).symlink_to('/offline-test-tools/' + package.name,target_is_directory=package.is_dir())
        name = 'coding-v11-offline-' + args.group + '-' + SOURCE[:12]
        paths = FOCUS if args.group == 'focus' else RECOVERY if args.group == 'recovery' else []
        initialize = "import sys,runpy; from pathlib import Path; Path('/tmp/import-data').mkdir(exist_ok=True); sys.argv=['pytest',*sys.argv[1:]]; runpy.run_module('pytest',run_name='__main__')"
        invocation = ['-B','-c',initialize,'-q','-p','no:cacheprovider',*paths,
            '--junitxml=/evidence/image-tests.xml','--basetemp=/tmp/pytest','--maxfail=3']
        if args.group == 'replay':
            script = '/work/docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/replay_saved_envelopes.py'
            program = '''from pathlib import Path
p = Path(SCRIPT_PATH)
s = p.read_text()
s = s.replace('ROOT = Path(__file__).resolve().parent', "ROOT = Path('/evidence/replay'); ROOT.mkdir(); HARNESS = Path('/work/docs/agent-harness-reset-v1')")
s = s.replace('ROOT.parent / "read-batch-scheduler-v1', 'HARNESS / "read-batch-scheduler-v1')
exec(compile(s,str(p),'exec'),{'__name__':'__main__','__file__':str(p)})
'''.replace('SCRIPT_PATH',repr(script))
            invocation = ['-B','-c',program]
        command = ['docker','create','--name',name,'--network','none','--read-only','--user','501:20',
            '--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','512m','--cpus','1','--pids-limit','128',
            '--log-driver','json-file','--log-opt','max-size=10m','--log-opt','max-file=1',
            '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777',
            '--mount','type=bind,src=' + str(work) + ',dst=/work,readonly',
            '--mount','type=bind,src=' + str(tools) + ',dst=/offline-test-tools,readonly',
            '--mount','type=bind,src=' + str(output) + ',dst=/evidence',
            '--workdir','/work','--env','PYTHONPATH=/app:/offline-test-tools:/work',
            '--env','PYTEST_DISABLE_PLUGIN_AUTOLOAD=1','--env','CONTRIBOS_CODING_BOUNDARY_EVIDENCE=/evidence/scenarios',
            '--env','DATABASE_URL=sqlite:////tmp/import-only.db','--env','ARTIFACT_ROOT=/tmp/import-artifacts',
            '--entrypoint','python',image,*invocation]
        cid = subprocess.check_output(command,text=True).strip()
        try:
            run = subprocess.run(['docker','start','-a',cid],capture_output=True,timeout=600)
            (Path('/tmp') / ('coding-v11-image-' + args.group + '.log')).write_bytes(run.stdout + run.stderr)
            code = docker('inspect',cid)[0]['State']['ExitCode']
            exported = Path('/tmp/coding-v11-image-' + args.group + ('.json' if args.group=='replay' else '.xml'))
            target = output / ('replay/saved-envelope-replay.json' if args.group=='replay' else 'image-tests.xml')
            assert target.is_file() and not target.is_symlink()
            shutil.copy2(target,exported)
            if args.group == 'replay':
                report = json.loads(exported.read_text())
                assert [r['child_sizes'] for r in report['replays']] == [[4,2],[4,1],[4,1],[4,4],[4,4],[4,4],[4,4]]
                shutil.copytree(output / 'replay/saved-envelope-replay',ROOT / 'saved-envelope-replay')
                shutil.copy2(exported,ROOT / 'saved-envelope-replay.json')
            # The exported cases are deterministic and contain no live task authority.
            if args.group == 'focus':
                shutil.copytree(output / 'scenarios','/tmp/coding-v11-image-scenarios')
            result = save('image-tests-' + args.group + '.json',{'image':image,'source_hash':SOURCE,
                'tracked_fixture_commit':HEAD,'pytest_tools_from_existing_local_cache':versions,
                'production_image_or_dependencies_modified':False,'network':'none','test_code_and_tools_read_only':True,
                'writable_mount':'dedicated_empty_temporary_evidence_directory_only',
                'child_test_cwd_aliases':'app resolves to /app/app; cached tools remain available after environment stripping; default data resolves to private tmpfs',
                'production_volumes_credentials_Docker_socket_or_HOME_mounted':False,'native_architecture':'arm64',
                'python_runtime':'deployed image Python 3.11','exit_code':code,'paid_model_calls':0,
                'temporary_fixture_databases_only':True,'group':args.group,'test_paths':paths,
                'export_raw_sha256':hashlib.sha256(exported.read_bytes()).hexdigest()})
            assert run.returncode == code == 0, 'offline_image_tests_failed'
            print(json.dumps({'image_tests':args.group,'exit_code':code,'image':image,'report_hash':result['report_hash']}))
        finally:
            subprocess.run(['docker','rm','-f',cid],capture_output=True,check=True)


if __name__ == '__main__': main()
