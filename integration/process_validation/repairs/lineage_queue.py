"""Recover earlier observations as unverified future work, never current facts."""
import json
import re

from .classifier import classify_failures
from .evidence import validate_report_binding


def previous_observations(folder, archive, manifest, selected):
    correction = manifest.get('correction') or {}
    root_id = correction.get('root_run_id')
    if not isinstance(root_id, str) or not re.fullmatch('[a-f0-9]{32}', root_id):
        return None
    root = folder.parent / root_id
    try:
        source = json.loads((root / 'run.json').read_text(encoding='utf8'))
        video = json.loads((root / 'video_manifest.json').read_text(encoding='utf8'))
        report = json.loads((root / 'process_validation/report.json').read_text(encoding='utf8'))
        if (source.get('snapshot', {}).get('run_id') != root_id or video.get('run_id') != root_id
                or validate_report_binding(root, report, root_id)):
            return None
        targets, blocked = classify_failures(report, root_id, manifest=video, archive=source)
        if blocked:
            return None
        steps = [s.get('step_id') for s in archive['snapshot']['plan']]
        boundary = max(steps.index(t.step_id) for t in selected)
        return [step for step in steps[boundary + 1:]
                if any(t.step_id == step for t in targets)]
    except (OSError, ValueError, TypeError, KeyError):
        return None
