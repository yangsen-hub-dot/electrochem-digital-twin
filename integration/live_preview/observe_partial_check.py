"""Observe real packets via the existing tunnel; no POST and no video download."""
import argparse
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
from PIL import Image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--timeout',type=int,default=900)
    args = parser.parse_args()
    output = args.output; output.mkdir(parents=True,exist_ok=True)
    token = args.token_file.read_text(encoding='utf-8-sig').strip()
    started = time.monotonic(); sequence = 0; observations = []; samples = []; revisions = set()
    last_print = 0; terminal = None; errors = 0
    while time.monotonic()-started < args.timeout:
        address = f'http://127.0.0.1:50007/preview?run_id={args.run_id}&after_sequence={sequence}'
        try:
            with urlopen(Request(address,headers={'Authorization':'Bearer '+token,'Origin':'null'}),timeout=15) as response:
                packet = json.load(response)
            if packet.get('run_id') != args.run_id:
                raise RuntimeError('Packet run mismatch')
            if packet.get('available') and not packet.get('unchanged'):
                if packet['sequence'] <= sequence:
                    raise RuntimeError('Non-monotonic preview sequence')
                image_bytes = base64.b64decode(packet['image_base64'],validate=True)
                with Image.open(BytesIO(image_bytes)) as image:
                    image.load()
                    if image.size != (packet['width'],packet['height']):
                        raise RuntimeError('Decoded picture dimensions do not match packet')
                state = packet['frame_state']
                observations.append({key:packet[key] for key in ('run_id','sequence','stream_status','age_s','stale','width','height')}
                                    | {'frame_state':state,'jpeg_sha256':hashlib.sha256(image_bytes).hexdigest(),
                                       'received_elapsed_s':round(time.monotonic()-started,3)})
                sequence = packet['sequence']
                revision = state['state_revision']
                if revision not in revisions and len(samples) < 8:
                    name = f'preview_revision_{revision}_frame_{state["frame_index"]}.jpg'
                    (output/name).write_bytes(image_bytes)
                    samples.append({'file':name,'sequence':sequence,'frame_state':state,
                                    'jpeg_sha256':hashlib.sha256(image_bytes).hexdigest()})
                    revisions.add(revision)
                (output/'observations.json').write_text(json.dumps(observations,ensure_ascii=False,indent=2),encoding='utf8')
                (output/'sample_frames.json').write_text(json.dumps(samples,ensure_ascii=False,indent=2),encoding='utf8')
            if packet.get('stream_status') in {'ended','failed'}:
                terminal = packet['stream_status']; break
        except Exception as exc:
            errors += 1
            if time.monotonic()-last_print > 10:
                print(json.dumps({'preview_wait':type(exc).__name__,'elapsed_s':round(time.monotonic()-started)}),flush=True)
        if time.monotonic()-last_print > 10:
            last = observations[-1] if observations else {}
            print(json.dumps({'packets':len(observations),'elapsed_s':round(time.monotonic()-started),
                              'last_frame':last.get('frame_state',{}).get('frame_index'),
                              'revision':last.get('frame_state',{}).get('state_revision')}),flush=True)
            last_print = time.monotonic()
        time.sleep(.35)
    report = {'run_id':args.run_id,'packet_count':len(observations),'terminal':terminal,
              'transport_errors':errors,'elapsed_s':round(time.monotonic()-started,3),
              'observed_revisions':sorted(revisions),'sample_jpegs':len(samples),'full_video_downloaded':False,
              'received_before_video_ready':any(row['stream_status']=='live' for row in observations)}
    (output/'live_observation_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report),flush=True)
    if terminal != 'ended' or len(revisions) < 3:
        raise SystemExit('Online prefix observation not accepted; inspect evidence before another run')


if __name__ == '__main__': main()
