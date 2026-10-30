"""Replay classification and optional recorded geometry; never run robot actions.

An index binds input files by SHA-256 and declares expected results. Missing
geometry is reported as uncovered, never converted into a collision-free pass.
"""
import argparse
import hashlib
import json
from pathlib import Path

from ..geometry_checks import check_trace
from ..repairs.classifier import classify_failures
from ..report import Report, atomic_json


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root, relative):
    path = (root / relative).resolve()
    if path == root or root not in path.parents:
        raise ValueError("回放输入路径超出样例目录")
    return path


def replay_case(index_root, case, policy):
    """Return an in-memory result; the source archive/report is never changed."""
    folder = _inside(index_root, case['directory'])
    bindings = case.get('sha256', {})
    required = {'run.json', 'video_manifest.json', 'process_validation/report.json'}
    if not required.issubset(bindings):
        raise ValueError("样例缺少运行、清单或原报告的哈希绑定")
    before = {}
    for relative, expected in bindings.items():
        path = _inside(folder, relative)
        actual = _digest(path)
        if actual != expected:
            raise ValueError(f"历史样例文件已变化：{case['case_id']} / {relative}")
        before[relative] = actual
    archive = json.loads((folder / 'run.json').read_text(encoding='utf-8'))
    manifest = json.loads((folder / 'video_manifest.json').read_text(encoding='utf-8'))
    saved = json.loads((folder / 'process_validation/report.json').read_text(encoding='utf-8'))
    run_id = case['run_id']
    if any(value != run_id for value in (
            folder.name, archive.get('snapshot', {}).get('run_id'), manifest.get('run_id'),
            saved.get('run_id'))):
        raise ValueError("历史样例运行编号不一致")
    targets, blocked = classify_failures(saved, run_id, manifest=manifest, archive=archive)
    classification = {
        'status': blocked['status'] if blocked else 'classified',
        'families': sorted({target.family for target in targets or []}),
        'step_ids': sorted({target.step_id for target in targets or []}),
        'reason': blocked.get('reason') if blocked else None,
    }
    differences = [f"classification.{key}" for key, expected in
                   case['expected']['classification'].items()
                   if classification.get(key) != expected]
    geometry_inputs = {'process_validation/geometry.json', 'process_validation/trace.jsonl'}
    geometry = {'status': 'uncovered', 'reason': '样例未绑定实际几何与轨迹，未重检碰撞'}
    if geometry_inputs.issubset(bindings):
        report = Report(run_id, policy)
        check_trace(folder / 'process_validation', archive, manifest, policy, report)
        data = report.finish()
        collisions = [row for row in data['issues']
                      if row['code'] == 'fixture_intersection' and row['status'] == 'fail']
        geometry = {'status': 'replayed_recorded_samples', 'counts': data['counts'],
                    'collision_issue_count': len(collisions),
                    'collision_step_ids': sorted({row.get('step_id') for row in collisions}),
                    'report': data}
    for key, expected in case['expected'].get('geometry', {}).items():
        if geometry.get(key) != expected:
            differences.append(f'geometry.{key}')
    if any(_digest(_inside(folder, name)) != digest for name, digest in before.items()):
        raise ValueError('历史样例在回放期间发生变化')
    return {'case_id': case['case_id'], 'label': case['label'], 'run_id': run_id,
            'status': 'pass' if not differences else 'fail', 'differences': differences,
            'classification': classification, 'geometry': geometry,
            'input_sha256': before, 'source_files_unchanged': True}


def replay_suite(index_path):
    index_path = Path(index_path).resolve()
    index = json.loads(index_path.read_text(encoding='utf-8'))
    cases = index.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('回放索引没有样例')
    if len({case['case_id'] for case in cases}) != len(cases):
        raise ValueError('回放样例编号重复')
    module_root = Path(__file__).parents[1]
    policy_path = module_root / 'policy.json'
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    results = []
    for case in cases:
        try:
            result = replay_case(index_path.parent, case, policy)
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            result = {'case_id': case.get('case_id'), 'status': 'fail', 'error': str(exc)}
        results.append(result)
    failed = sum(result['status'] != 'pass' for result in results)
    return {'schema_version': '1.0', 'scope': 'offline_classification_and_recorded_geometry',
            'index_sha256': _digest(index_path), 'status': 'fail' if failed else 'pass',
            'policy_sha256': _digest(policy_path),
            'checker_source_sha256': {name: _digest(module_root / name) for name in (
                'replay/runner.py', 'geometry_checks.py', 'geometry.py', 'geometry_evidence.py', 'report.py',
                'repairs/classifier.py', 'repairs/evidence.py', 'repairs/pose_checks.py')},
            'counts': {'pass': len(results) - failed, 'fail': failed}, 'cases': results,
            'does_not_execute_actions': True,
            'interpretation': '通过表示结果符合已登记预期；失败样例应继续检出异常。'
                              '未生成新轨迹、未解码视频、不证明候选可达或连续无碰撞。'}


def main():
    parser = argparse.ArgumentParser(description='离线回放历史分类和已有几何，不启动仿真')
    parser.add_argument('--index', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    index = args.index.resolve()
    output = args.output.resolve()
    # Keep outputs entirely outside the input suite so no evidence can be overwritten.
    if output == index.parent or index.parent in output.parents:
        parser.error('输出文件必须保存在历史样例目录之外')
    result = replay_suite(index)
    atomic_json(output, result)
    print(json.dumps({'status': result['status'], 'counts': result['counts']}, ensure_ascii=True))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
