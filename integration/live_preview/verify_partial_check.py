"""Cloud-side comparison of observed frame packets, receipts and the cloud MP4."""
import argparse
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import subprocess
from PIL import Image, ImageChops, ImageStat
import imageio_ffmpeg


def close(actual, expected, name, tolerance=1e-8):
    if actual is None or expected is None:
        if actual != expected: raise ValueError('Missing value mismatch: '+name)
    elif not math.isclose(float(actual),float(expected),rel_tol=1e-10,abs_tol=tolerance):
        raise ValueError('Receipt/frame mismatch: '+name)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run-id',required=True)
    p.add_argument('--evidence',type=Path,required=True)
    args = p.parse_args()
    root = Path('/mnt/data/electrochem_twin/runs')/args.run_id
    archive = json.loads((root/'run.json').read_text())
    manifest = json.loads((root/'video_manifest.json').read_text())
    observed = json.loads((args.evidence/'observations.json').read_text())
    samples = json.loads((args.evidence/'sample_frames.json').read_text())
    receipts = {row['result']['state_revision']:row['result'] for row in archive['accepted_receipts'].values()}
    if manifest['status'] != 'segment_diagnostic_ready' or archive['snapshot']['full_experiment_completed']:
        raise ValueError('Not an isolated diagnostic prefix')
    if archive['snapshot']['completed_steps'] != 5 or not archive['snapshot']['conservation']['passed']:
        raise ValueError('Five-action diagnostic did not finish with conservation')
    for packet in observed:
        if packet['run_id'] != args.run_id: raise ValueError('Wrong run')
        state = packet['frame_state']; frame = manifest['frames'][state['frame_index']]
        close(state['simulation_time_s'],frame['simulation_time_s'],'frame time')
        if state['phase'] != frame['phase'] or state['step_id'] != frame['step_id']:
            raise ValueError('Frame/phase/step mismatch')
        ledger = receipts[state['state_revision']]
        if state['completed_steps'] != ledger['completed_steps']: raise ValueError('Progress mismatch')
        for side in ('left','right'):
            cell = ledger['state']['containers'][side+'_half_cell']
            shown = state['cells'][side]
            close(shown['volume_ul'],cell['volume_ul'],side+' volume')
            close(shown['mixing_uniformity'],cell['mixing_uniformity'],side+' uniformity')
            for species,value in shown['concentrations_m'].items():
                close(value,cell['concentrations_m'][species],side+' '+species,1e-10)
        if state['voltage_valid'] or state['virtual_meter_mv'] is not None:
            raise ValueError('Prefix must not report a measured voltage')
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    video = root/'first_transfer.mp4'
    comparisons = []
    for sample in samples:
        frame_index = sample['frame_state']['frame_index']
        data = (args.evidence/sample['file']).read_bytes()
        if hashlib.sha256(data).hexdigest() != sample['jpeg_sha256']: raise ValueError('Sample SHA mismatch')
        png = subprocess.check_output([ffmpeg,'-v','error','-i',str(video),'-vf',f'select=eq(n\\,{frame_index})',
                                       '-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'])
        with Image.open(BytesIO(data)) as preview, Image.open(BytesIO(png)) as encoded:
            full = encoded.convert('RGB'); full.thumbnail(preview.size)
            mae = sum(ImageStat.Stat(ImageChops.difference(preview.convert('RGB'),full)).mean)/3
            if mae > 8: raise ValueError(f'Preview/recording frame mismatch, MAE={mae}')
            comparisons.append({'frame_index':frame_index,'state_revision':sample['frame_state']['state_revision'],
                                'mean_absolute_pixel_difference_0_255':round(mae,5)})
    decoded = subprocess.run([ffmpeg,'-v','error','-i',str(video),'-f','null','-'],capture_output=True,text=True)
    if decoded.returncode or decoded.stderr.strip(): raise ValueError('Cloud MP4 full decode failed')
    report = {'run_id':args.run_id,'scope':archive['snapshot']['scope'],
              'completed_steps':archive['snapshot']['completed_steps'],'total_steps':archive['snapshot']['total_steps'],
              'full_experiment_completed':False,'conservation_passed':True,
              'observed_packet_count':len(observed),'all_observed_frames_match_manifest_and_confirmed_receipts':True,
              'sample_preview_vs_mp4_comparisons':comparisons,'video_status':manifest['status'],
              'video_frames':manifest['frame_count'],'video_duration_s':manifest['duration_s'],
              'cloud_video_path':str(video),'cloud_video_bytes':video.stat().st_size,
              'cloud_video_sha256':hashlib.sha256(video.read_bytes()).hexdigest(),
              'cloud_full_decode_passed':True,'full_video_downloaded':False}
    (root/'live_preview_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__': main()
