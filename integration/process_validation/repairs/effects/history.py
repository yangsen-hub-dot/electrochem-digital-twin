"""Reject the same ineffective commands even if candidate labels change."""
import json
import re
from pathlib import Path

from .fingerprints import candidate_fingerprint
from .identities import failures
from ..evidence import validate_report_binding


def repeated_candidate(run_dir, proposal, archive):
    folder = Path(run_dir).resolve()
    wanted = candidate_fingerprint(proposal, archive)
    target_steps = set(proposal.get('repair_step_ids', []))
    seen, checked = set(), []
    for _ in range(64):
        if folder.name in seen:
            return {'blocked': True, 'reason': '修复历史出现循环引用'}
        seen.add(folder.name)
        try:
            manifest = json.loads((folder/'video_manifest.json').read_text(encoding='utf-8'))
            current = json.loads((folder/'run.json').read_text(encoding='utf-8'))
            report = json.loads((folder/'process_validation/report.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'blocked': False, 'history_complete': False, 'checked_runs': checked}
        if any(value != folder.name for value in (manifest.get('run_id'), current.get('snapshot', {}).get('run_id'), report.get('run_id'))):
            return {'blocked': True, 'reason': '修复历史档案编号不一致'}
        binding = validate_report_binding(folder, report, folder.name)
        if binding:
            return {'blocked': False, 'history_complete': False, 'checked_runs': checked,
                    'reason': '历史报告未绑定当前证据，不据此认定重复失败'}
        correction = manifest.get('correction') or {}
        checked.append(folder.name)
        if correction and candidate_fingerprint(correction, current) == wanted:
            if any(key[2] in target_steps for key in failures(report, current)):
                return {'blocked': True, 'reason': '同一实际参数方案已在相同条件下失败，停止重复无效修复',
                        'matched_run_id': folder.name, 'candidate_fingerprint': wanted}
        parent = correction.get('source_run_id')
        if not parent: return {'blocked': False, 'history_complete': True, 'checked_runs': checked}
        if not isinstance(parent, str) or not re.fullmatch('[a-f0-9]{32}', parent):
            return {'blocked': True, 'reason': '修复历史来源编号无效'}
        folder = folder.parent/parent
    return {'blocked': True, 'reason': '修复谱系超出有界历史检查范围'}
