import json, os, subprocess, tempfile
with tempfile.TemporaryDirectory() as directory:
    env={'PATH':os.environ['PATH'],'HOME':directory,'CODEX_HOME':directory,'LANG':'C.UTF-8'}
    result=subprocess.run(['codex','--version'],env=env,capture_output=True,text=True,timeout=10)
    p=subprocess.Popen(['codex','app-server'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    p.stdin.write(json.dumps({'id':1,'method':'initialize','params':{'clientInfo':{'name':'arv2-spike','version':'1'},'capabilities':{'experimentalApi':True}}})+'\n');p.stdin.flush()
    import select
    ready,_,_=select.select([p.stdout],[],[],10)
    assert ready, 'initialization timeout'
    response=json.loads(p.stdout.readline()); assert response.get('id')==1 and 'result' in response
    p.terminate();p.wait(timeout=5)
    print(json.dumps({'classification':'protocol_spike_no_model_or_credentials','cli_version':result.stdout.strip(),'app_server_initialize':True,'model_tool_loop':'not_exercised','external_writes':0},indent=2))
