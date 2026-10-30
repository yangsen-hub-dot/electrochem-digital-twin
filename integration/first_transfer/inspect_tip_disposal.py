"""Offline QA of one continuous run, including unchanged vessels and residual accounting.

Extracts representative *existing video frames*, not replacement images/video.
Run in the cloud's Matterix Python after the MP4 has finalized.
"""
import argparse
import json
import math
from pathlib import Path


def inspect(folder):
    archive = json.loads((folder / 'run.json').read_text(encoding='utf-8'))
    manifest = json.loads((folder / 'video_manifest.json').read_text(encoding='utf-8'))
    final = archive['snapshot']
    fixture = manifest['presentation']['tip_rack']
    assert fixture['model'] == 'commercial_96_tip_rack_visual_adapter'
    assert fixture['tip_count'] == 96 and fixture['rows'] == 8 and fixture['columns'] == 12
    assert math.isclose(fixture['pitch_m'], .009, abs_tol=1e-12)
    assert fixture['dimension_status'].startswith('single-rack envelope is project estimate')
    assert math.isclose(fixture['housing_bottom_z_m'], fixture['desktop_z_m'], abs_tol=1e-12)
    assert fixture['rack_top_z_m'] > fixture['rack_bottom_z_m']
    assert math.isclose(fixture['tip_length_m'], .084, abs_tol=1e-12)
    events = [e for e in archive['events'] if e['type'] == 'step.completed']
    assert events[-1]['action'] == 'discard_tip'
    assert final['status'] == 'completed' and final['video']['status'] == 'ready'
    assert manifest['run_id'] == final['run_id']
    previous = archive['accepted_receipts'][events[-2]['payload']['command_id']]['result']
    before, after = previous['state'], final['state']
    assert before['tip']['attached'] and not after['tip']['attached']
    assert after['tip']['liquid']['volume_ul'] == 0
    assert before['containers'] == after['containers']
    assert math.isclose(after['waste']['volume_ul'], before['waste']['volume_ul'] + before['tip']['liquid']['volume_ul'], abs_tol=1e-9)
    for species in set(before['tip']['liquid']['amounts_mol']) | set(before['waste']['amounts_mol']):
        assert math.isclose(after['waste']['amounts_mol'].get(species, 0),
            before['waste']['amounts_mol'].get(species, 0) + before['tip']['liquid']['amounts_mol'].get(species, 0), abs_tol=1e-15)
    assert previous['error_model_realized'] == final['error_model_realized']
    assert final['conservation']['passed']
    assert not final['voltage']['valid']
    assert all(e['run_id'] == final['run_id'] for e in archive['events'])
    import imageio.v2 as imageio
    reader = imageio.get_reader(folder / 'first_transfer.mp4')
    metadata = reader.get_meta_data()
    assert list(metadata['size']) == [1920, 1440]
    selected = {}
    qa = folder / 'disposal_qa'
    qa.mkdir(exist_ok=True)
    for phase in ('load_tip/dock', 'dispense/confirmed', 'discard_tip/position', 'discard_tip/fall', 'discard_tip/confirmed'):
        frames = [f for f in manifest['frames'] if f['phase'] == phase]
        assert frames, phase
        frame = frames[len(frames)//2]
        name = phase.replace('/', '_') + '.png'
        imageio.imwrite(qa / name, reader.get_data(frame['frame']))
        selected[phase] = {'file': name, 'frame': frame['frame'], 'video_time_s': frame['video_time_s']}
    reader.close()
    report = {'run_id': final['run_id'], 'completed_steps': final['completed_steps'],
              'checks': {'tip_detached': True, 'existing_residual_to_waste_once': True,
                         'vessels_unchanged_during_disposal': True, 'no_new_error_profile': True,
                         'conservation': True, 'same_run_id': True, 'resolution_1920x1440': True},
              'waste_volume_ul': after['waste']['volume_ul'],
              'left_volume_ul': after['containers']['left_half_cell']['volume_ul'],
              'duration_s': manifest['duration_s'], 'representative_frames': selected,
              'tip_disposal': manifest['tip_disposal'], 'tip_rack_contact': fixture,
              'boundary': 'semantic kinematic release/drop; not contact or fluid validation; visual QA separate'}
    (qa / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('run_dir', type=Path)
    print(json.dumps(inspect(parser.parse_args().run_dir), ensure_ascii=False))
