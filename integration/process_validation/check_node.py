"""Read-only tunnel/API checks. Never submit or replay an experiment."""
import argparse
import json
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen


def check_node(endpoint,media_endpoint,token):
    results=[]
    def request(name,url,*,authorized=True,method='GET',expected=200):
        headers={'Origin':'null'}
        if authorized:headers['Authorization']='Bearer '+token
        if method=='OPTIONS':headers.update({'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization,content-type'})
        try:
            with urlopen(Request(url,headers=headers,method=method),timeout=15) as response:
                data=response.read();code=response.status;cors=response.headers.get('Access-Control-Allow-Origin')
                payload=json.loads(data) if data else {}
        except HTTPError as exc:code=exc.code;payload={};cors=None
        except (OSError,URLError,ValueError) as exc:
            results.append({'check':name,'passed':False,'message':str(exc)});return {}
        passed=code==expected
        if expected==200 and method=='GET' and name not in {'coordinator_health','media_health'}:passed &= cors=='null'
        results.append({'check':name,'passed':passed,'http_status':code,'cors_origin':cors})
        return payload
    request('coordinator_health',endpoint+'/health',authorized=False)
    request('media_health',media_endpoint+'/media/health',authorized=False)
    request('unauthorized_state',endpoint+'/state',authorized=False,expected=401)
    request('validation_post_preflight',endpoint+'/validation/start',authorized=False,method='OPTIONS',expected=204)
    state=request('state',endpoint+'/state')
    caps=request('capabilities',endpoint+'/capabilities')
    request('history',endpoint+'/history')
    request('batch_status',endpoint+'/batch/status')
    recordings=request('recordings',media_endpoint+'/media/runs')
    for name in ('batch','history','process_validation','live_preview'):
        value=caps.get(name)
        results.append({'check':'capability_'+name,'passed':value is True or isinstance(value,dict) and value.get('available') is True})
    return {'passed':all(row['passed'] for row in results),'checks':results,'run_id':state.get('run_id'),
            'run_status':state.get('status'),'recordings_count':len(recordings.get('recordings',[])),
            'note':'只读检查；原实验暂停/失败与网络连通性分别报告'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--endpoint',default='http://127.0.0.1:50007')
    p.add_argument('--media-endpoint',default='http://127.0.0.1:50006')
    p.add_argument('--token-file',type=Path,required=True)
    p.add_argument('--output',type=Path)
    a=p.parse_args();r=check_node(a.endpoint.rstrip('/'),a.media_endpoint.rstrip('/'),a.token_file.read_text(encoding='utf-8-sig').strip())
    if a.output:
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(r,ensure_ascii=False));return 0 if r['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
