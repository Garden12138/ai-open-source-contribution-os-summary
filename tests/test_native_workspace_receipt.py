from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import pytest

from app.node_environments import validate_node_receipt
from tests.test_node_environments import node_files, selected


def native_receipt():
    recipe=selected(node_files("npm","10.9.4"))["node_environment"]
    return {"version":"node-environment-result-v2","recipe":recipe,"runtime_version":"22.0.0",
        "runtime_bin":"/usr/local/bin","manager_version":"10.9.4","manifest_hashes":{},
        "built_offline":True,"execution_image_digest":"sha256:"+"a"*64,
        "native_packages":[{"package":"@fixture/native-linux-arm64","version":"1.2.3",
            "workspace_directory":"packages/lib","binary_name":"fixture","main":"fixture.linux-arm64.node",
            "binary_hash":"a"*64}]}


def test_native_workspace_receipt_is_exact_and_keeps_legacy_compatibility():
    value=native_receipt()
    assert validate_node_receipt(value)==value
    legacy=deepcopy(value)
    legacy.pop("native_packages");legacy["version"]="node-environment-result-v1"
    assert validate_node_receipt(legacy)==legacy


def cache_receipt():
    value=native_receipt()
    path='project/node_modules/.pnpm/vitest@2.1.9/node_modules/vitest/dist/chunks/tsconfig.tmp.tsbuildinfo'
    value.update(version='node-environment-result-v3',native_packages=[],cache_files=[{
        'path':path,'target':'/execution/cache/node/'+hashlib.sha256(path.encode()).hexdigest()+'.tsbuildinfo',
        'adapter':'vitest-2.1.9-typecheck-v1'}])
    return value


def test_cache_receipt_and_supervisor_only_materialize_exact_data_link(tmp_path):
    from pathlib import Path
    from app.sandbox_worker.node_cache import prepare_node_cache_files
    value=cache_receipt()
    assert validate_node_receipt(value)==value
    output=tmp_path/'output';project=output/'project'
    cache=value['cache_files'][0]
    target=output/cache['path'];package=target.parent.parent.parent
    target.parent.mkdir(parents=True)
    (package/'package.json').write_text(json.dumps({'name':'vitest','version':'2.1.9'}))
    implementation=target.parent/'typecheck.js';implementation.write_text('const name = "tsconfig.tmp.tsbuildinfo";')
    before=implementation.read_bytes()
    assert prepare_node_cache_files(str(project),str(output))==value['cache_files']
    assert target.is_symlink() and os.readlink(target)==cache['target']
    assert implementation.read_bytes()==before
    with pytest.raises(ValueError):prepare_node_cache_files(str(project),str(output))


@pytest.mark.parametrize('change',['escape','target','code_file','adapter','duplicate','extra_field','empty'])
def test_cache_receipt_rejects_widened_or_unbound_writes(change):
    value=cache_receipt();cache=value['cache_files'][0]
    if change=='escape':cache['path']=cache['path'].replace('project/','project/../')
    elif change=='target':cache['target']='/dependencies/output/tools/node_modules/.bin/node'
    elif change=='code_file':cache['path']=cache['path'].replace('tsconfig.tmp.tsbuildinfo','package.json')
    elif change=='adapter':cache['adapter']='arbitrary-file-write'
    elif change=='duplicate':value['cache_files'].append(deepcopy(cache))
    elif change=='extra_field':cache['command']='sh'
    elif change=='empty':value['cache_files']=[]
    with pytest.raises(ValueError):validate_node_receipt(value)


def test_cache_adapter_rejects_link_traversal_and_ignores_other_tool_versions(tmp_path):
    from app.sandbox_worker.node_cache import prepare_node_cache_files
    output=tmp_path/'output';project=output/'project';package=project/'node_modules/vitest'
    package.mkdir(parents=True);manifest=package/'package.json'
    manifest.write_text(json.dumps({'name':'vitest','version':'3.0.0'}))
    assert prepare_node_cache_files(str(project),str(output))==[]
    manifest.write_text(json.dumps({'name':'vitest','version':'2.1.9'}))
    outside=tmp_path/'outside';outside.mkdir()
    (package/'dist').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError):prepare_node_cache_files(str(project),str(output))
    assert not list(outside.iterdir())


@pytest.mark.parametrize("field,value",[("main","../../escape.node"),("binary_hash","wrong"),
    ("package","https://example.com/addon"),("version","latest"),("workspace_directory","outside")])
def test_native_workspace_receipt_rejects_unbound_or_unsafe_packages(field,value):
    receipt=native_receipt();receipt["native_packages"][0][field]=value
    with pytest.raises(ValueError):validate_node_receipt(receipt)


@pytest.mark.parametrize("entrypoint", ["native/index.js", "../escape.js", "/tmp/escape.js", "node_modules/pkg/index.js"])
def test_native_link_uses_private_scope_without_writing_frozen_modules(tmp_path, monkeypatch, entrypoint):
    from app.sandbox_worker.node_execution import prepare_node_execution
    from types import SimpleNamespace
    import subprocess
    receipt = native_receipt()
    receipt["recipe"]["workspace_lifecycle"] = []
    source, dependencies, scratch = (tmp_path / name for name in ("source", "dependencies", "scratch"))
    for path in (source, dependencies, scratch): path.mkdir()
    (source / "index.js").write_text("export const value = 1;\n")
    workspace = source / "packages/lib"
    workspace.mkdir(parents=True)
    (workspace / "package.json").write_text(json.dumps({"name":"@fixture/native", "version":"1.2.3",
        "napi":{"binaryName":"fixture"}, "main":entrypoint}))
    (workspace / "native").mkdir()
    (workspace / "native/index.js").write_text("// controlled loader source\n")
    frozen_scope = dependencies / "project/node_modules/@fixture"
    (frozen_scope / "lib").mkdir(parents=True)
    native = receipt["native_packages"][0]
    binary = dependencies / "native/node_modules" / native["package"] / native["main"]
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"controlled binary fixture, never executed")
    native["binary_hash"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    (dependencies / "node-environment.json").write_text(json.dumps(receipt))
    def probe(argv, **kwargs):
        kwargs["stdout"].write(("v22.0.0" if argv[0] == "node" else "10.9.4").encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", probe)
    if entrypoint != "native/index.js":
        with pytest.raises(ValueError, match="Invalid native workspace entrypoint"):
            prepare_node_execution(str(source), str(dependencies), str(scratch), receipt["recipe"], {})
        assert sorted(p.name for p in frozen_scope.iterdir()) == ["lib"]
        return
    target, _, _ = prepare_node_execution(str(source), str(dependencies), str(scratch), receipt["recipe"], {})
    from pathlib import Path
    scope = Path(target) / "node_modules/@fixture"
    assert scope.is_dir() and not scope.is_symlink()
    assert (scope / "lib").resolve() == frozen_scope / "lib"
    assert (scope / "native-linux-arm64").resolve() == binary.parent
    assert (Path(target) / "packages/lib/native" / native["main"]).resolve() == binary
    assert not (workspace / "native" / native["main"]).exists()
    assert sorted(p.name for p in frozen_scope.iterdir()) == ["lib"]


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="real native package acquisition is opt-in")
def test_real_bun_napi_workspace_loads_frozen_binary_through_fresh_verify(tmp_path, monkeypatch):
    from tests.test_node_workflow import node_workflow, verify_to_preview
    files = node_files("npm", "10.9.4")
    files.pop("package-lock.json")
    package = json.loads(files["package.json"])
    package["packageManager"] = "bun@1.4.4"
    package["scripts"]["test"] = "bun test.mjs"
    files["package.json"] = json.dumps(package)
    files["packages/natives/package.json"] = json.dumps({"name":"@oh-my-pi/pi-natives", "version":"18.12.6",
        "napi":{"binaryName":"pi_natives"}, "main":"./native/index.cjs"})
    files["packages/natives/native/index.cjs"] = "module.exports = require('./pi_natives.linux-'+process.arch+'.node');\n"
    files["packages/other/package.json"] = json.dumps({"name":"@oh-my-pi/other", "version":"1.0.0"})
    files["test.mjs"] += ("import {createRequire} from 'node:module'; "
        "const addon=createRequire(import.meta.url)('./packages/natives'); "
        "assert.equal(typeof addon,'object'); console.log('native-addon-loaded');\n")
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, "bun", "1.4.4",
        real=True, files_override=files)
    verify_to_preview(db, settings, task, workers, code, review)
