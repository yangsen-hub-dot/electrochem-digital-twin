"""Cloud-only diagnostic launcher. One explicitly authorized five-action prefix."""
import json
import os
from pathlib import Path
import shlex
import subprocess
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    base = Path('/mnt/data/electrochem_twin')
    current = (base/'current').resolve()
    runtime = '/home/ubuntu/miniforge3/envs/matterix/bin/python'
    token = (base/'media_service/access_token.txt').read_text().strip()
    def get(path):
        with urlopen(Request('http://127.0.0.1:50007'+path, headers={'Authorization':'Bearer '+token}),timeout=10) as response:
            return json.load(response)
    state, batch = get('/state'), get('/batch/status')
    if state.get('status') in {'starting','running','finalizing','paused'} or batch.get('status') in {'running','paused','cancelling'}:
        raise SystemExit('Diagnostic not started: an experiment/batch is active')
    if not get('/capabilities').get('live_preview',{}).get('available'):
        raise SystemExit('Preview capability missing')
    # Refuse a second invocation rather than silently consuming another GPU run.
    receipt_path = base/'jobs/live_preview_partial_check_20261022.json'
    if receipt_path.exists():
        raise SystemExit('Diagnostic receipt already exists; inspect it instead of launching again')
    processes = subprocess.check_output(['ps','-eo','args'],text=True).splitlines()
    if any('matterix_first_transfer.py' in line and 'cloud_experiment_server.py' not in line for line in processes):
        raise SystemExit('Diagnostic not started: another standalone renderer is active')
    run_id = uuid4().hex
    conditions = base/'jobs'/('preview_check_'+run_id+'.conditions.json')
    conditions.write_text(json.dumps(state['conditions'],ensure_ascii=False,indent=2),encoding='utf8')
    manifest = json.loads((current/'deployment_manifest.json').read_text())
    env = {
        'MATTERIX_PATH':str(current/manifest['matterix_relative_path']),
        'OMNI_KIT_ACCEPT_EULA':'YES', 'ELECTROCHEM_GPU_ID':'0', 'ELECTROCHEM_CPU_THREADS':'4',
        'OMP_NUM_THREADS':'4',
    }
    ffmpeg = subprocess.check_output([runtime,'-c','import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())'],text=True).strip()
    env['IMAGEIO_FFMPEG_EXE'] = ffmpeg
    argv = [runtime,'-u',str(current/'integration/first_transfer/matterix_first_transfer.py'),
            '--engine-root',str(current/'electrochemistry_simulation'), '--conditions',str(conditions),
            '--output-root',str(base/'runs'), '--run-id',run_id,'--seed','20261022',
            '--include-paired-mix','--include-left-electrode','--include-right-electrode',
            '--include-salt-bridge','--include-measurement','--stop-after-actions','5','--live-preview']
    log = base/'logs'/('preview_check_'+run_id+'.log')
    exit_file = base/'jobs'/('preview_check_'+run_id+'.exit')
    command = ('cd '+shlex.quote(str(current))+' && '+shlex.join(['env',*(k+'='+v for k,v in env.items()),*argv])+
               ' > '+shlex.quote(str(log))+' 2>&1; result=$?; printf "%s" "$result" > '+shlex.quote(str(exit_file))+'; exit "$result"')
    receipt = {'run_id':run_id,'release':current.name,'max_actions':5,'cloud_log':str(log),
               'cloud_exit_file':str(exit_file),'cloud_run_dir':str(base/'runs'/run_id),
               'scope':'initial_state_prefix_diagnostic','full_workflow_submitted':False,
               'note':'Standalone prefix; production coordinator stays on its prior run. Preview this explicit run_id.'}
    # Atomic exclusive receipt prevents accidental duplicate launches.
    with receipt_path.open('x',encoding='utf8') as stream:
        json.dump(receipt,stream,ensure_ascii=False,indent=2)
    subprocess.run(['tmux','new-session','-d','-s','preview-check-'+run_id[:8],command],check=True)
    print(json.dumps(receipt,ensure_ascii=False))


if __name__ == '__main__': main()
