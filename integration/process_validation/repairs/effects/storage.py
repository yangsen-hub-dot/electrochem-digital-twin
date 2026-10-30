"""Bind both reports to their immutable local evidence before evaluating effects."""
import json
import re
from pathlib import Path

from ..evidence import validate_report_binding
from .comparison import compare_records
from ..dependency_plan import validate_dependency_profile


def read_record(folder):
    folder = Path(folder).resolve()
    record = {key: json.loads((folder/name).read_text(encoding='utf-8')) for key, name in (
        ('archive', 'run.json'), ('manifest', 'video_manifest.json'), ('report', 'process_validation/report.json'))}
    if any(value != folder.name for value in (record['archive'].get('snapshot', {}).get('run_id'),
                                              record['manifest'].get('run_id'), record['report'].get('run_id'))):
        raise ValueError('比较输入的目录、档案、清单或报告运行编号不一致')
    return record


def evaluate_repair(run_dir, report=None, *, during_audit=False):
    folder = Path(run_dir).resolve()
    try:
        after = read_record(folder)
        if report is not None and report != after['report']:
            raise ValueError('当前复验报告已变化')
        source = (after['manifest'].get('correction') or {}).get('source_run_id')
        if not isinstance(source, str) or not re.fullmatch('[a-f0-9]{32}', source) or source == folder.name:
            raise ValueError('修复来源编号无效或循环引用')
        before = read_record(folder.parent/source)
        for record, path, finished in ((before, folder.parent/source, True), (after, folder, not during_audit)):
            failure = validate_report_binding(path, record['report'], path.name, require_finished=finished)
            if failure: raise ValueError(failure['reason'])
        correction = after['manifest']['correction']
        validate_dependency_profile({**correction, 'actions': correction.get('parameters') or {}},
                                    before['archive'])
        result = compare_records(before, after)
        result['source_revisions'] = {'before': before['report']['source_revisions'],
                                      'after': after['report']['source_revisions']}
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return {'schema_version': '1.0', 'run_id': folder.name, 'status': 'inconclusive',
                'eligible_for_continuation': False, 'reasons': [str(exc)]}
