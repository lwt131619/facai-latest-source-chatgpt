"""Isolated, non-root Docker service/restart QA via container-local HTTP only.

Build facai-production-increment:local first. This does not certify host port
mapping, cross-device access, actual agent execution or provider availability.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CLIENT = '''
import hashlib,http.cookiejar,io,json,os,urllib.request,zipfile
jar=http.cookiejar.CookieJar()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar))
def request(path,body=None,headers=None,binary=False):
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request('http://127.0.0.1:8080'+path,data=data,headers={'Content-Type':'application/json',**(headers or {})})
    with opener.open(req,timeout=4) as response:return response.read() if binary else json.load(response)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='facai-production-increment:local')
    args = parser.parse_args()
    env = dict(os.environ, FACAI_ADMIN_PASSWORD=secrets.token_urlsafe(24))
    # Docker's default config directory can be read-only in cloud workspaces.
    config = ROOT/'dist/docker-smoke-config'; config.mkdir(parents=True, exist_ok=True)
    env['DOCKER_CONFIG'] = str(config)

    def docker(*command, source=None):
        result = subprocess.run(['docker', *command], env=env, input=source,
                                text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError('Docker QA command failed: '+result.stderr[-1500:])
        return result.stdout.strip()

    name = 'facai-qa-'+uuid4().hex[:12]
    cid = None
    docker('volume', 'create', name)
    try:
        command = ['run', '-d', '--name', name, '-v', name+':/app/data',
                   '-e', 'FACAI_ADMIN_PASSWORD', '-e', 'FACAI_DEFAULT_MONTHLY_LIMIT_USD=1000',
                   '-e', 'FACAI_SCHEDULER_ENABLED=0', '-e', 'FACAI_CHANNEL_NOTIFY_ENABLED=0']
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'):
            if env.get(key): command.extend(['-e', key])
        if env.get('CODEX_PROXY_CERT'):
            command.extend(['-v', env['CODEX_PROXY_CERT']+':/run/facai-proxy-ca.crt:ro',
                            '-e', 'SSL_CERT_FILE=/run/facai-proxy-ca.crt'])
        cid = docker(*command, args.image)

        def wait():
            for _ in range(60):
                try:
                    result = docker('exec', '-i', cid, 'python', '-', source=CLIENT+"\nprint(json.dumps(request('/health')))\n")
                    if json.loads(result)['auth_required']: return
                except (RuntimeError, ValueError): time.sleep(.25)
            logs = docker('logs', '--tail', '30', cid)
            # These are isolated synthetic credentials, but do not print logs
            # blindly: the caller may have added sensitive middleware.
            raise RuntimeError('Container health failed; '+str(len(logs))+' diagnostic log characters available')

        wait()
        assert docker('exec', cid, 'id', '-u') == '10001'
        state = json.loads(docker('exec', '-i', cid, 'python', '-', source=CLIENT+'''
session=request('/api/v1/auth/login',{'username':'admin','password':os.environ['FACAI_ADMIN_PASSWORD']})
headers={'X-CSRF-Token':session['csrf_token']}
intent=request('/api/v1/intents',{'raw_request':'分析这个项目','interpretation':{'action':'analyze','target':'/qa/repo'}},headers)
mission=request('/api/v1/intents/'+intent['id']+'/confirm',{'expected_revision':1},headers)['mission_id']
endpoint='/api/v1/missions/'+mission+'/trust'
request(endpoint+'/evidence',{'expected_revision':0,'source':'QA Owner note','content':'Synthetic container QA evidence'},headers)
proof=request(endpoint+'/proofs',{'expected_revision':1},headers)
archive=request(endpoint+'/proofs/'+proof['id']+'/download',binary=True)
with zipfile.ZipFile(io.BytesIO(archive)) as z:
    assert hashlib.sha256(z.read('proof.json')).hexdigest()==proof['sha256']
print(json.dumps({'mission':mission,'intent':intent['id'],'cookie':'; '.join(c.name+'='+c.value for c in jar)}))
'''))  # Session cookie is captured in memory, never printed or written.
        docker('restart', cid); wait()
        source = CLIENT+'\nstate=json.loads('+repr(json.dumps(state))+')\n'+'''
headers={'Cookie':state['cookie']}
trust=request('/api/v1/missions/'+state['mission']+'/trust',headers=headers)
assert trust['revision']==1 and len(trust['evidence'])==1
spec=request('/api/v1/missions/'+state['mission']+'/specification',headers=headers)
assert spec['specification']['intent_id']==state['intent']
assert 'EXECUTION_NOT_COMPLETED' in trust['verification']['acceptance_blockers']
assert request('/api/v1/auth/session',headers=headers)['authenticated']
print('PASS')
'''
        assert docker('exec', '-i', cid, 'python', '-', source=source) == 'PASS'
        report = {'docker_service': 'PASS', 'uid': 10001, 'health_authentication': True,
                  'same_session_after_restart': True, 'mission_specification_restored': True,
                  'evidence_restored': True, 'proof_export_hash_matches': True,
                  'live_provider_calls': False, 'host_port_access': 'NOT_VERIFIED_IN_CLOUD_NAMESPACE',
                  'budget': 'Explicit QA fixture 1000 USD; production default remains unchanged'}
        (ROOT/'dist/docker-increment-smoke.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report))
    finally:
        if cid: docker('rm', '-f', cid)
        docker('volume', 'rm', name)


if __name__ == '__main__':
    main()
