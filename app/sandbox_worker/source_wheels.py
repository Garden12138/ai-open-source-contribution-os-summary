"""Versioned source acquisition and offline compilation programs for Runner only."""

import re
from inspect import getsource
from pathlib import PurePosixPath
from app.security import ensure_no_sensitive_data


def validate_source_build_request(value: object) -> dict:
    keys={'version','package','package_version','source_hash','source_directory','build_root','python_version'}
    if not isinstance(value,dict) or set(value)!=keys or value.get('version')!='source-wheel-request-v1':
        raise ValueError('Invalid source build request')
    if (not isinstance(value['package'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}',value['package'])
            or not isinstance(value['package_version'],str) or not re.fullmatch(r'[A-Za-z0-9_.+!-]{1,100}',value['package_version'])
            or not isinstance(value['source_hash'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['source_hash'])
            or value['python_version'] not in [[3,11],[3,12],[3,13],[3,14]]):
        raise ValueError('Invalid source build identity')
    root='source-build-inputs/'+value['package']
    if value['build_root']!=root or not isinstance(value['source_directory'],str):
        raise ValueError('Invalid source build path')
    path=PurePosixPath(value['source_directory'])
    if path.is_absolute() or '..' in path.parts or not str(path).startswith(root+'/source/') or '\\' in str(path) or str(path)!=value['source_directory'] or any(ord(c)<32 or ord(c)==127 for c in str(path)):
        raise ValueError('Invalid source build path')
    ensure_no_sensitive_data(value,context='source build request')
    return value


def extract_source_archive(archive_path: str, extracted: str, *, max_bytes: int = 128000000,
                           max_entries: int = 20000) -> None:
    """Data-only extraction, shared verbatim with the trusted acquisition program."""
    import tarfile
    import pathlib
    import shutil
    total=0
    with tarfile.open(archive_path,'r:*') as bundle:
        for count,member in enumerate(bundle,1):
            path=pathlib.PurePosixPath(member.name)
            if (count>max_entries or path.is_absolute() or '..' in path.parts or not path.parts
                    or '\\' in member.name or any(ord(c)<32 for c in member.name)
                    or not (member.isfile() or member.isdir())):
                raise ValueError('Unsafe source archive')
            total+=member.size
            if total>max_bytes: raise ValueError('Source archive budget exceeded')
            target=pathlib.Path(extracted).joinpath(*path.parts)
            if member.isdir(): target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with bundle.extractfile(member) as incoming,target.open('xb') as output:
                    shutil.copyfileobj(incoming,output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)

SOURCE_ACQUISITION_SCRIPT = getsource(extract_source_archive) + r'''
def request_source_build(package, requirements):
    import urllib.request, urllib.parse, urllib.error, tarfile, pathlib
    def stop(reason):
        print(json.dumps({'version':'dependency-failure-v1','phase':'source_acquire','reason_code':reason,'exit_code':1,'packages':[package]}))
        raise SystemExit(103)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",package): raise SystemExit(103)
    class Redirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): raise ValueError("redirect")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https":proxy}),Redirects())
    def fetch(url, limit):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"pypi.org","files.pythonhosted.org"} or parsed.username or parsed.password or parsed.port not in {None,443}:
            raise SystemExit(103)
        try:
            with opener.open(url,timeout=30) as response:
                value = response.read(limit+1)
        except urllib.error.HTTPError as error:
            stop('proxy_denied' if error.code==403 else 'source_distribution_missing' if error.code==404 else 'network_unavailable')
        except (OSError,ValueError): stop('network_unavailable')
        if len(value)>limit: raise SystemExit(103)
        return value
    project_index = json.loads(fetch("https://pypi.org/pypi/"+package+"/json",8000000))
    selector = """
import json,sys
from pip._vendor.packaging.version import Version, InvalidVersion
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.specifiers import SpecifierSet
data=json.load(sys.stdin); name=data['package'].lower().replace('_','-').replace('.','-')
constraints=[]
for text in data['requirements']:
    r=Requirement(text)
    if r.name.lower().replace('_','-').replace('.','-')==name and (r.marker is None or r.marker.evaluate()): constraints.append(r.specifier)
candidates=[]
for version,files in data['index']['releases'].items():
    try: v=Version(version)
    except InvalidVersion: continue
    if v.is_prerelease or any(not s.contains(v) for s in constraints): continue
    for f in files:
        if f.get('packagetype')!='sdist' or f.get('yanked'): continue
        if f.get('requires_python') and not SpecifierSet(f['requires_python']).contains('.'.join(map(str,sys.version_info[:3]))): continue
        candidates.append((v,f))
if not candidates: sys.exit(1)
v,f=max(candidates,key=lambda p:p[0]); print(json.dumps({'version':str(v),'url':f['url'],'sha256':f['digests']['sha256']}))
"""
    chosen = subprocess.run([venv+"/bin/python","-I","-c",textwrap.dedent(selector)],
        input=json.dumps({"package":package,"requirements":requirements,"index":project_index}).encode(),
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=environment)
    if chosen.returncode or len(chosen.stdout)>4096: stop('source_distribution_missing')
    selected=json.loads(chosen.stdout)
    if not re.fullmatch(r"[0-9a-f]{64}",selected['sha256']): raise SystemExit(103)
    source=fetch(selected['url'],32000000)
    if hashlib.sha256(source).hexdigest()!=selected['sha256']: stop('source_hash_mismatch')
    source_root=dependency_root+"/source-build-inputs/"+normalize(package)
    if os.path.exists(source_root): raise SystemExit(103)
    os.makedirs(source_root)
    archive_path=source_root+"/source.tar.gz"
    with open(archive_path,"wb") as handle: handle.write(source)
    extracted=source_root+"/source"
    os.makedirs(extracted)
    try: extract_source_archive(archive_path,extracted)
    except (ValueError,OSError,tarfile.TarError): stop('source_archive_invalid')
    roots=os.listdir(extracted)
    if len(roots)!=1 or not os.path.isdir(extracted+'/'+roots[0]): raise SystemExit(103)
    source_dir=extracted+'/'+roots[0]
    pyproject=source_dir+'/pyproject.toml'
    build_requires=['setuptools>=64','wheel']
    if os.path.isfile(pyproject):
        with open(pyproject,'rb') as handle: metadata=tomllib.load(handle)
        metadata.setdefault('build-system',{}).setdefault('build-backend','setuptools.build_meta')
        if python_backend_error(metadata): stop('source_metadata_invalid')
        build_requires=metadata.get('build-system',{}).get('requires',[])
    if not isinstance(build_requires,list) or len(build_requires)>100 or any(python_requirement_error(r) for r in build_requires): raise SystemExit(103)
    # A dedicated source-build venv retains the exact build-tool lock. No
    # upstream build hook executes in this online acquisition container.
    build_root=source_root+'/tools'
    os.makedirs(build_root)
    requested=source_root+'/build-requirements.txt'
    with open(requested,'w') as handle: handle.write('\n'.join(build_requires)+'\n')
    run([*pip,'download','--only-binary=:all:','--no-cache-dir','--index-url','https://pypi.org/simple',
         '--proxy',proxy,'--dest',build_root,'-r',requested],95,phase='build_tools_resolve')
    lock=source_root+'/build-requirements.lock'
    lines=[]
    for filename in sorted(os.listdir(build_root)):
        if not filename.endswith('.whl'): raise SystemExit(103)
        with zipfile.ZipFile(build_root+'/'+filename) as wheel:
            names=[n for n in wheel.namelist() if n.count('/')==1 and n.endswith('.dist-info/METADATA')]
            if len(names)!=1 or wheel.getinfo(names[0]).file_size>2000000: raise SystemExit(103)
            info=email.message_from_bytes(wheel.read(names[0]))
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',info.get('Name','')) or not re.fullmatch(r'[A-Za-z0-9_.+!-]+',info.get('Version','')): raise SystemExit(103)
        with open(build_root+'/'+filename,'rb') as handle: digest=hashlib.file_digest(handle,'sha256').hexdigest()
        lines.append(info['Name']+'=='+info['Version']+' --hash=sha256:'+digest)
    with open(lock,'w') as handle: handle.write('\n'.join(lines)+'\n')
    tool_venv=source_root+'/venv'
    run(['python3','-m','venv',tool_venv],94,phase='create_venv')
    run([tool_venv+'/bin/python','-m','pip','--isolated','--disable-pip-version-check','install',
         '--no-index','--find-links',build_root,'--require-hashes','--no-cache-dir','-r',lock],97,phase='build_tools_install')
    os.makedirs(dependency_root+'/source-build-output/'+normalize(package))
    request={'version':'source-wheel-request-v1','package':normalize(package),'package_version':selected['version'],
        'source_hash':selected['sha256'],'source_directory':source_dir[len(dependency_root)+1:],
        'build_root':source_root[len(dependency_root)+1:],'python_version':list(sys.version_info[:2])}
    with open(source_root+'/request.json','w') as handle: json.dump(request,handle,sort_keys=True)
    print(json.dumps(request)); raise SystemExit(102)
'''.strip()

OFFLINE_SOURCE_BUILD_SCRIPT = r'''
import base64,email,hashlib,json,os,pathlib,re,subprocess,sys,zipfile,shutil
request=json.loads(sys.argv[1])
source_root=pathlib.Path('/source-inputs')
if hashlib.sha256((source_root/'source.tar.gz').read_bytes()).hexdigest()!=request['source_hash']: raise SystemExit(103)
if list(sys.version_info[:2])!=request['python_version']: raise SystemExit(103)
environment={'HOME':'/tmp','LANG':'C.UTF-8','PATH':'/usr/local/bin:/usr/bin:/bin',
    'PIP_CONFIG_FILE':'/dev/null','PYTHONDONTWRITEBYTECODE':'1',
    **{k:os.environ[k] for k in __GIT_ENVIRONMENT_NAMES__}}
def run(argv):
    p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=environment)
    tail=b''
    while chunk:=p.stdout.read(8192): tail=(tail+chunk)[-65536:]
    status=p.wait()
    if status:
        print(json.dumps(summarize_dependency_failure('upstream_source_build',status,tail.decode('utf-8','replace'))))
        raise SystemExit(104)
venv=source_root/'venv'
pip=[str(venv/'bin/python'),'-m','pip','--isolated','--disable-pip-version-check']
source_relative=request['source_directory'][len(request['build_root'])+1:]
project=pathlib.Path('/tmp/source-project')
shutil.copytree(source_root/source_relative,project,symlinks=False)
output=pathlib.Path('/tmp/source-wheels')
output.mkdir()
run([*pip,'wheel','--no-index','--no-deps','--no-build-isolation','--wheel-dir',str(output),str(project)])
wheels=list(output.iterdir())
if len(wheels)!=1 or wheels[0].suffix!='.whl': raise SystemExit(103)
with zipfile.ZipFile(wheels[0]) as wheel:
    metadata=[n for n in wheel.namelist() if n.count('/')==1 and n.endswith('.dist-info/METADATA')]
    if len(metadata)!=1 or wheel.getinfo(metadata[0]).file_size>2000000: raise SystemExit(103)
    info=email.message_from_bytes(wheel.read(metadata[0]))
normalize=lambda n: re.sub(r'[-_.]+','-',n).lower()
if normalize(info.get('Name',''))!=request['package'] or info.get('Version')!=request['package_version']: raise SystemExit(103)
target=wheels[0]
receipt={'version':'source-wheel-built-v1','request':request,'wheel':target.name,
    'wheel_hash':hashlib.sha256(target.read_bytes()).hexdigest(),
    'build_lock_hash':hashlib.sha256((source_root/'build-requirements.lock').read_bytes()).hexdigest()}
if target.stat().st_size>32000000: raise SystemExit(103)
print(json.dumps({'receipt':receipt,'wheel_data':base64.b64encode(target.read_bytes()).decode('ascii')}))
'''.strip()

IMPORT_SOURCE_WHEEL_SCRIPT = r'''
import base64,hashlib,json,os,pathlib,sys
value=json.loads(pathlib.Path('/input/wheel.json').read_bytes())
receipt=value['receipt']; request=receipt['request']; package=request['package']
root=pathlib.Path('/dependencies/output/source-build-output')/package
if root.is_symlink() or not root.is_dir() or os.path.realpath(root)!=str(root): raise SystemExit(103)
filename=receipt['wheel']
if os.path.basename(filename)!=filename or not filename.endswith('.whl'): raise SystemExit(103)
data=base64.b64decode(value['wheel_data'],validate=True)
if len(data)>32000000 or hashlib.sha256(data).hexdigest()!=receipt['wheel_hash']: raise SystemExit(103)
wheels=root/'wheels'; wheels.mkdir()
with (wheels/filename).open('xb') as handle: handle.write(data)
(root/'receipt.json').write_text(json.dumps(receipt,sort_keys=True,separators=(',',':')))
print(json.dumps({'version':'source-wheel-built-v1','package':package}))
'''.strip()
