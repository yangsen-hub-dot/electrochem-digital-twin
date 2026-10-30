"""Recognize a finished correction segment without claiming a complete video."""
from __future__ import annotations

import json
from pathlib import Path
from .repairs.evidence import validate_report_binding, report_rows


def terminal_correction_verified(run_dir):
    folder = Path(run_dir)
    try:
        archive = json.loads((folder/'run.json').read_text(encoding='utf-8'))
        manifest = json.loads((folder/'video_manifest.json').read_text(encoding='utf-8'))
        report = json.loads((folder/'process_validation/report.json').read_text(encoding='utf-8'))
        snapshot = archive['snapshot']
        if any(value != folder.name for value in (snapshot.get('run_id'), manifest.get('run_id'), report.get('run_id'))):
            return False
        if validate_report_binding(folder, report, folder.name):
            return False
        if (snapshot.get('status') != 'completed' or snapshot.get('state_uncertain') is not False
                or archive.get('pending_command') or snapshot.get('video',{}).get('status') != 'ready'
                or manifest.get('status') != 'segment_diagnostic_ready'
                or not manifest.get('correction') or manifest.get('continuation')):
            return False
        steps = snapshot.get('plan', [])
        events = [e for e in archive.get('events', []) if e.get('type') == 'step.completed']
        count = snapshot.get('completed_steps')
        if (not steps or isinstance(count, bool) or not isinstance(count, int) or count != len(steps)
                or count != snapshot.get('state_revision') or count != len(events)
                or [e.get('step_id') for e in events] != [s.get('step_id') for s in steps]
                or steps[-1].get('action') != 'measure_voltage'):
            return False
        executed = manifest.get('diagnostic',{}).get('executed_step_ids',[])
        if not executed or executed[-1] != steps[-1].get('step_id'):
            return False
        rows = report_rows(report)
        allowed_nonpass = {('records','scope','review'),('coverage','not_implemented','uncovered')}
        if any(row.get('status') != 'pass' and (row.get('group'),row.get('code'),row.get('status')) not in allowed_nonpass for row in rows):
            return False
        passed = {(row.get('group'),row.get('code')) for row in rows if row.get('status')=='pass'}
        required = {('records','run_identity'),('records','protocol_order'),('ledger','ledger_revision'),
                    ('ledger','repair_lineage'),('ledger','repair_contract'),('ledger','conservation'),
                    ('video','timeline'),('video','full_decode'),('video','resolution'),('video','encoded_count'),
                    ('sync','trace_length'),('sync','confirmed_ledger'),('geometry','coverage'),
                    ('geometry','sampled_intersections'),('geometry','registered_repair_pose')}
        return required.issubset(passed)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False
