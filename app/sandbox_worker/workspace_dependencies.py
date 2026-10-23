"""Trusted container program for declarative Python workspace preparation.

Runs only inside the credential-free package-proxy container. The resolved
wheelhouse and hash lock are retained in the immutable dependency artifact.
"""

from inspect import getsource
from app.execution_preflight import python_project_error, python_requirement_error, python_backend_error, python_development_requirements
from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure
from app.sandbox_worker.source_wheels import SOURCE_ACQUISITION_SCRIPT
from app.sandbox_worker.python_dependency_check import check_python_dependency_versions
from app.sandbox_worker.package_environments import python_package_environments

_WORKSPACE_CHECK = getsource(check_python_dependency_versions) + '''
import importlib.metadata, json, sys
configuration=json.load(sys.stdin)
packages={d.metadata['Name']:[d.version,d.requires or []] for d in importlib.metadata.distributions()}
selected=configuration.get('selected_requirements',{})
if selected:
    from packaging.utils import canonicalize_name
    packages={canonicalize_name(name):value for name,value in packages.items()}
    for name,declarations in selected.items():
        if name not in packages:
            print('ResolutionImpossible: conflicting dependencies'); sys.exit(1)
        packages[name][1].extend(declarations)
result=check_python_dependency_versions(packages,configuration['local_versions'],configuration['override_edges'])
if result['failures']:
    print('ResolutionImpossible: conflicting dependencies')
    sys.exit(1)
print(json.dumps(result,sort_keys=True,separators=(',',':')))
'''

WORKSPACE_PYTHON_SCRIPT = getsource(python_requirement_error) + getsource(python_backend_error) + getsource(python_development_requirements) + getsource(python_project_error) + getsource(summarize_dependency_failure) + r'''
import email, re, tomllib, zipfile, textwrap, sys
dependency_root = globals().get("environment_output_root", "/dependencies/output")
scratch_prefix = globals().get("environment_scratch_prefix", "")
os.makedirs(dependency_root, exist_ok=True)
project_dir = plan["project_directory"]
project = os.path.join("/workspace", project_dir)
primary_dirs = plan.get("project_directories") or [project_dir]
project_dirs = [*primary_dirs, *plan.get("local_projects", [])]
metadatas = {}
requirements = []
dynamic_mode = plan.get("metadata_strategy") == "wheel-metadata-v1"
dynamic_projects = set()
include_development = plan.get("preparation_version", "python-workspace-preparation-v1") in {"python-workspace-preparation-v2", "python-workspace-preparation-v3"}
multi_package = plan.get("preparation_version") == "python-workspace-preparation-v3"
selected_requirements = {}
def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()
for directory in project_dirs:
    with open(os.path.join("/workspace", directory, "pyproject.toml"), "rb") as handle:
        metadata = tomllib.load(handle)
    project_metadata = metadata.get("project", {})
    if python_project_error(metadata, allow_dynamic=dynamic_mode, include_development=include_development):
        raise SystemExit(91)
    name = normalize(project_metadata["name"])
    if name in metadatas:
        raise SystemExit(91)
    metadatas[name] = (directory, metadata)
    first_requirement = len(requirements)
    if set(project_metadata.get("dynamic", [])) & {"dependencies", "optional-dependencies"}:
        dynamic_projects.add(name)
    requirements += list(project_metadata.get("dependencies", []))
    requirements += list(metadata.get("build-system", {}).get("requires", []))
    if include_development:
        requirements += python_development_requirements(metadata)
    extras = project_metadata.get("optional-dependencies", {})
    for group in ("test", "tests", "instruments", "type-check"):
        requirements += list(extras.get(group, []))
    if multi_package:
        selected_requirements[name] = list(requirements[first_requirement:])
requirements += ["pytest", "pytest-asyncio", "pytest-socket"]
for manifest in plan["manifests"]:
    if manifest["path"].endswith((".txt", ".lock")):
        with open(os.path.join("/workspace", manifest["path"]), encoding="utf-8") as handle:
            declarations = [line.split(" #", 1)[0].strip() for line in handle
                            if line.strip() and not line.lstrip().startswith("#")]
        requirements += declarations
        if multi_package:
            first_name = next(name for name, (directory, _) in metadatas.items() if directory == primary_dirs[0])
            selected_requirements[first_name] += declarations
if len(requirements) > 300:
    raise SystemExit(92)
for requirement in requirements:
    # Do not let a manifest inject pip flags, local paths, VCS URLs or indexes.
    if python_requirement_error(requirement):
        raise SystemExit(93)
requirements = [r for r in requirements if normalize(re.match(r"^([A-Za-z0-9_.-]+)", r).group(1)) not in metadatas]
venv = dependency_root + "/venv"
wheelhouse = dependency_root + "/wheels"
os.makedirs(wheelhouse,exist_ok=True)
def run(argv, code, cwd=None, phase=None, allow_failure=False):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=environment, cwd=cwd)
    tail = b""
    while chunk := process.stdout.read(8192):
        tail = (tail + chunk)[-65536:]
    status = process.wait()
    if allow_failure:
        return status,tail.decode("utf-8","replace")
    if status:
        phases = {94:"create_venv",95:"runtime_resolve",97:"runtime_install",
                  98:"project_build",99:"project_install"}
        print(json.dumps(summarize_dependency_failure(phase or phases[code],status,tail.decode("utf-8","replace"))))
        raise SystemExit(code)
run(["python3", "-m", "venv", venv], 94)
# PEP 517 backends may locate their installed executable through PATH. Keep
# those tools inside this credential-free venv, ahead of the fixed system path.
environment["PATH"] = venv + "/bin:" + environment["PATH"]
environment["VIRTUAL_ENV"] = venv
pip = [venv + "/bin/python", "-m", "pip", "--isolated", "--disable-pip-version-check"]
inputs = dependency_root + "/requested-requirements.txt"
with open(inputs, "w", encoding="utf-8") as handle:
    handle.write("\n".join(requirements) + "\n")
local_wheels = dependency_root + "/local-wheels"
if os.path.isdir(local_wheels): shutil.rmtree(local_wheels)
os.makedirs(local_wheels)
built_dynamic = {}
metadata_receipts = []
if dynamic_projects:
    # Acquire only declared build tools first. Repository build hooks execute
    # solely in this credential-free, resource-bounded preparation container.
    # Upstream dependencies remain wheel-only behind the same fixed proxy.
    bootstrap = dependency_root + "/bootstrap-wheels"
    os.makedirs(bootstrap,exist_ok=True)
    build_requirements = ["wheel"]
    for name in sorted(dynamic_projects):
        build_requirements += metadatas[name][1].get("build-system", {}).get("requires", [])
    if len(build_requirements) > 100 or any(python_requirement_error(r) for r in build_requirements):
        raise SystemExit(93)
    build_input = dependency_root + "/build-requirements.txt"
    with open(build_input, "w", encoding="utf-8") as handle:
        handle.write("\n".join(build_requirements) + "\n")
    run([*pip, "download", "--only-binary=:all:", "--no-cache-dir", "--index-url", "https://pypi.org/simple",
         "--proxy", proxy, "--dest", bootstrap, "-r", build_input], 95, phase="build_tools_resolve")
    bootstrap_lock = dependency_root + "/build-requirements.lock"
    bootstrap_lines = []
    for filename in sorted(os.listdir(bootstrap)):
        path = os.path.join(bootstrap, filename)
        if not filename.endswith(".whl"): raise SystemExit(96)
        with zipfile.ZipFile(path) as wheel:
            names = [n for n in wheel.namelist() if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
            if len(names) != 1 or wheel.getinfo(names[0]).file_size > 2000000: raise SystemExit(96)
            info = email.message_from_bytes(wheel.read(names[0]))
        if not info["Name"] or not info["Version"] or not re.fullmatch(r"[A-Za-z0-9_.-]+", info["Name"]) or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", info["Version"]):
            raise SystemExit(96)
        with open(path,"rb") as handle: digest = hashlib.file_digest(handle,"sha256").hexdigest()
        bootstrap_lines.append(info["Name"] + "==" + info["Version"] + " --hash=sha256:" + digest)
    with open(bootstrap_lock,"w",encoding="utf-8") as handle: handle.write("\n".join(bootstrap_lines) + "\n")
    run([*pip,"install","--no-index","--find-links",bootstrap,"--require-hashes","--no-cache-dir","-r",bootstrap_lock],97,phase="build_tools_install")
    for index, name in enumerate(sorted(dynamic_projects)):
        source = "/tmp/" + scratch_prefix + "metadata-project-" + str(index)
        shutil.copytree(os.path.join("/workspace", metadatas[name][0]), source, symlinks=False)
        previous = set(os.listdir(local_wheels))
        run([*pip,"wheel","--no-index","--no-deps","--no-build-isolation","--wheel-dir",local_wheels,source],98,phase="project_metadata")
        created = set(os.listdir(local_wheels)) - previous
        if len(created) != 1: raise SystemExit(100)
        filename = created.pop()
        if not filename.endswith(".whl"): raise SystemExit(100)
        with zipfile.ZipFile(os.path.join(local_wheels,filename)) as wheel:
            names = [n for n in wheel.namelist() if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
            if len(names) != 1 or wheel.getinfo(names[0]).file_size > 2000000: raise SystemExit(100)
            raw_metadata = wheel.read(names[0])
            info = email.message_from_bytes(raw_metadata)
        if normalize(info.get("Name", "")) != name: raise SystemExit(100)
        discovered = info.get_all("Requires-Dist", [])
        if len(discovered) > 300: raise SystemExit(92)
        if any(python_requirement_error(declaration) for declaration in discovered): raise SystemExit(93)
        # Runner's system interpreter intentionally has no pip. Parse markers
        # with the already-created venv's bundled parser, without importing code
        # from the selected source tree or changing the Runner image.
        parser = """
import json, sys
from pip._vendor.packaging.requirements import Requirement
selected = []
for declaration in json.load(sys.stdin):
    requirement = Requirement(declaration)
    if requirement.marker is None or any(requirement.marker.evaluate({"extra":group})
            for group in ("", "test", "tests", "instruments", "type-check")):
        selected.append(str(requirement).split(";",1)[0].strip())
print(json.dumps(selected))
"""
        parsed = subprocess.run([venv+"/bin/python","-I","-c",textwrap.dedent(parser)], input=json.dumps(discovered).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, check=False)
        if parsed.returncode or len(parsed.stdout)>1000000: raise SystemExit(100)
        discovered_requirements = json.loads(parsed.stdout)
        requirements += discovered_requirements
        if multi_package:
            selected_requirements[name] += discovered_requirements
        built_dynamic[name] = filename
        metadata_receipts.append({"name":name,"metadata_hash":hashlib.sha256(raw_metadata).hexdigest(),
                                  "requires_dist":discovered,"wheel":filename})
    requirements = [r for r in requirements if normalize(re.match(r"^([A-Za-z0-9_.-]+)",r).group(1)) not in metadatas]
    if len(requirements) > 300 or any(python_requirement_error(r) for r in requirements): raise SystemExit(93)
    with open(inputs,"w",encoding="utf-8") as handle: handle.write("\n".join(requirements)+"\n")
    with open(dependency_root+"/project-metadata.json","w",encoding="utf-8") as handle:
        json.dump(metadata_receipts,handle,sort_keys=True,separators=(",",":"))
__SOURCE_ACQUISITION__
source_mode=plan.get('distribution_strategy')=='source-wheel-v1'
source_cache_root=globals().get('shared_source_root',dependency_root)
source_wheels=dependency_root+'/built-source-wheels'
os.makedirs(source_wheels,exist_ok=True)
if source_mode:
    output_root=source_cache_root+'/source-build-output'
    if os.path.isdir(output_root):
        for package in sorted(os.listdir(output_root)):
            receipt_path=output_root+'/'+package+'/receipt.json'
            if not os.path.isfile(receipt_path): raise SystemExit(103)
            with open(receipt_path) as handle: receipt=json.load(handle)
            filename=receipt['wheel']
            if os.path.basename(filename)!=filename or not filename.endswith('.whl'): raise SystemExit(103)
            built=output_root+'/'+package+'/wheels/'+filename
            with open(built,'rb') as handle: digest=hashlib.file_digest(handle,'sha256').hexdigest()
            if digest!=receipt['wheel_hash']: raise SystemExit(103)
            destination=source_wheels+'/'+filename
            if not os.path.exists(destination): shutil.copyfile(built,destination)
resolve_argv=[*pip, "download", "--only-binary=:all:", "--no-cache-dir",
     "--index-url", "https://pypi.org/simple", "--proxy", proxy,
     "--dest", wheelhouse, "-r", inputs]
if source_mode:
    resolve_argv += ['--find-links',source_wheels]
    status,tail=run(resolve_argv,95,allow_failure=True)
    if status:
        diagnostic=summarize_dependency_failure('runtime_resolve',status,tail)
        if diagnostic['reason_code']=='no_matching_distribution' and diagnostic['packages']:
            package=diagnostic['packages'][0]
            missing=re.findall(r'(?:No matching distribution found for |Could not find a version that satisfies the requirement )([^\n]+)',tail)
            requested=list(requirements)
            for declaration in missing:
                declaration=declaration.split(' (from ',1)[0].strip()
                if not python_requirement_error(declaration): requested.append(declaration)
            previous_root=dependency_root
            dependency_root=source_cache_root
            try:
                request_source_build(package,requested)
            finally:
                dependency_root=previous_root
        print(json.dumps(diagnostic)); raise SystemExit(95)
else:
    run(resolve_argv,95)
locked = []
for filename in sorted(os.listdir(wheelhouse)):
    path = os.path.join(wheelhouse, filename)
    if not filename.endswith(".whl") or os.path.getsize(path) > plan["disk_bytes"]:
        raise SystemExit(96)
    with zipfile.ZipFile(path) as wheel:
        names = [name for name in wheel.namelist() if name.count("/") == 1 and name.endswith(".dist-info/METADATA")]
        if len(names) != 1 or wheel.getinfo(names[0]).file_size > 2000000:
            raise SystemExit(96)
        info = email.message_from_bytes(wheel.read(names[0]))
    name, version = info["Name"], info["Version"]
    if not name or not version or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", version):
        raise SystemExit(96)
    with open(path, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    locked.append(name + "==" + version + " --hash=sha256:" + digest)
lock = dependency_root + "/resolved-requirements.lock"
with open(lock, "w", encoding="utf-8") as handle:
    handle.write("\n".join(locked) + "\n")
run([*pip, "install", "--no-index", "--find-links", wheelhouse,
     "--require-hashes", "--no-cache-dir", "-r", lock], 97)
# Build only the selected package, offline, in a bounded disposable directory.
# Build backends are untrusted and get the same resource and network boundary.
ordered = []
visiting = set()
def visit(name):
    if name in ordered: return
    if name in visiting: raise SystemExit(91)
    visiting.add(name)
    for requirement in metadatas[name][1].get("project", {}).get("dependencies", []):
        match = re.match(r"^([A-Za-z0-9_.-]+)", requirement)
        dependency_name = normalize(match.group(1)) if match else ""
        if dependency_name in metadatas: visit(dependency_name)
    visiting.remove(name)
    ordered.append(name)
for name in metadatas: visit(name)
for index, name in enumerate(ordered):
    if name in built_dynamic:
        run([*pip,"install","--no-index","--no-deps",os.path.join(local_wheels,built_dynamic[name])],99)
        continue
    source = "/tmp/" + scratch_prefix + "build-project-" + str(index)
    shutil.copytree(os.path.join("/workspace", metadatas[name][0]), source, symlinks=False)
    previous = set(os.listdir(local_wheels))
    run([*pip, "wheel", "--no-index", "--no-deps", "--no-build-isolation",
         "--wheel-dir", local_wheels, source], 98)
    created = set(os.listdir(local_wheels)) - previous
    if len(created) != 1: raise SystemExit(98)
    for filename in created:
        if not filename.endswith(".whl"): raise SystemExit(98)
        run([*pip, "install", "--no-index", "--no-deps", os.path.join(local_wheels, filename)], 99)
if not include_development:
    run([*pip, "check"], 99, phase="dependency_check")
else:
    local_versions = {}
    override_edges = []
    for filename in sorted(os.listdir(local_wheels)):
        with zipfile.ZipFile(os.path.join(local_wheels,filename)) as wheel:
            names=[n for n in wheel.namelist() if n.count('/')==1 and n.endswith('.dist-info/METADATA')]
            if len(names)!=1 or wheel.getinfo(names[0]).file_size>2000000: raise SystemExit(100)
            info=email.message_from_bytes(wheel.read(names[0]))
        name=normalize(info.get('Name',''))
        version=info.get('Version','')
        if name not in metadatas or not re.fullmatch(r'[A-Za-z0-9_.+!-]+',version): raise SystemExit(100)
        declared=metadatas[name][1].get('project',{}).get('version')
        if declared is not None and declared!=version: raise SystemExit(100)
        local_versions[name]=version
    for name,(_,metadata) in metadatas.items():
        sources=metadata.get('tool',{}).get('uv',{}).get('sources',{})
        if not isinstance(sources,dict): raise SystemExit(91)
        for target,value in sources.items():
            if isinstance(value,dict) and 'workspace' in value:
                if value!={'workspace':True} or normalize(target) not in metadatas: raise SystemExit(91)
                override_edges.append([name,normalize(target)])
    checked=subprocess.run([venv+'/bin/python','-I','-c',__WORKSPACE_DEPENDENCY_CHECK__],
        input=json.dumps({'local_versions':local_versions,'override_edges':override_edges,
                         **({'selected_requirements':selected_requirements} if multi_package else {})}).encode(),
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=environment,check=False)
    if checked.returncode or len(checked.stdout)>1000000:
        print(json.dumps(summarize_dependency_failure('dependency_check',1,checked.stdout.decode('utf-8','replace'))))
        raise SystemExit(99)
    # Preserve exact source overrides in the hashed environment inventory.
    with open(dependency_root+'/workspace-dependency-check.json','wb') as handle: handle.write(checked.stdout)
# Source precedence ensures Verify exercises the modified package, rather than
# the frozen baseline wheel; the wheel provides metadata/entry points only.
source_paths = []
for directory in project_dirs:
    source_path = os.path.join("/workspace", directory, "src")
    if not os.path.isdir(source_path): source_path = os.path.join("/workspace", directory)
    source_paths.append(source_path)
with open(dependency_root + "/source-paths.json", "w", encoding="utf-8") as handle:
    json.dump(source_paths, handle)
if multi_package:
    with open(dependency_root + "/selected-projects.json", "w", encoding="utf-8") as handle:
        json.dump({"project_directories":primary_dirs,"local_projects":plan.get("local_projects",[]),
                   "selected_requirements":selected_requirements}, handle, sort_keys=True, separators=(",",":"))
'''.strip().replace('__SOURCE_ACQUISITION__',SOURCE_ACQUISITION_SCRIPT).replace('__WORKSPACE_DEPENDENCY_CHECK__',repr(_WORKSPACE_CHECK))

MULTIPACKAGE_PYTHON_SCRIPT = getsource(python_package_environments) + r'''
import tomllib
all_metadata = {}
manifest_texts = {}
for manifest in plan['manifests']:
    with open('/workspace/' + manifest['path'], encoding='utf-8') as handle:
        manifest_texts[manifest['path']] = handle.read()
for directory in [*plan['project_directories'], *plan.get('local_projects', [])]:
    path = ('' if directory == '.' else directory + '/') + 'pyproject.toml'
    all_metadata[directory] = tomllib.loads(manifest_texts[path])
try:
    per_package = python_package_environments(plan, all_metadata, manifest_texts)
except (ValueError, TypeError, KeyError):
    raise SystemExit(91)
package_receipts = []
for index, package_plan in enumerate(per_package):
    output = '/dependencies/output/environments/' + str(index)
    child = {'plan':package_plan, 'environment':dict(environment), 'proxy':proxy,
             'hashlib':hashlib, 'json':json, 'os':os, 'shutil':shutil, 'stat':stat,
             'subprocess':subprocess, 'environment_output_root':output,
             'environment_scratch_prefix':'package-' + str(index) + '-',
             'shared_source_root':'/dependencies/output'}
    exec(__PACKAGE_PREPARATION_PROGRAM__, child)
    package_receipts.append({'project_directory':package_plan['project_directory'],
        'output_directory':'environments/' + str(index),
        'manifests':package_plan['manifests'], 'local_projects':package_plan['local_projects']})
dependency_root = '/dependencies/output'
with open(dependency_root + '/package-environments.json', 'w', encoding='utf-8') as handle:
    json.dump({'version':'python-package-environments-v1', 'packages':package_receipts},
              handle, sort_keys=True, separators=(',',':'))
'''.strip().replace('__PACKAGE_PREPARATION_PROGRAM__', repr(WORKSPACE_PYTHON_SCRIPT))
