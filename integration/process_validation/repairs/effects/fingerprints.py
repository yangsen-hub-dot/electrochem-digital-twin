"""Stable identities for checker versions and executable candidate parameters."""
import hashlib
import json
from pathlib import Path


def checker_fingerprints(policy):
    root = Path(__file__).resolve().parents[2]
    files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(root.rglob('*.py'))
             if not p.name.startswith('test_') and '__pycache__' not in p.parts}
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {'checker_fingerprint': digest(files), 'policy_fingerprint': digest(policy)}


def candidate_fingerprint(profile, archive):
    def canonical(value, key=None):
        if isinstance(value, dict):
            return {k: canonical(v, k) for k, v in sorted(value.items())
                    if k not in {'candidate_id', 'evidence_obstacles'}}
        if isinstance(value, list):
            result = [canonical(v, key) if isinstance(v, list) else canonical(v) for v in value]
            if (key and 'orientation' in key and key.endswith('wxyz') and len(result) == 4
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in result)):
                first = next((v for v in result if abs(v) > 1e-12), 0.)
                if first < 0: result = [-v for v in result]
            return result
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(float(value), 12)
        return value
    correction = profile.get('repair_contract') or {}
    dependency = profile.get('dependency_plan') or correction.get('dependency_plan') or {}
    signature = {'actions': profile.get('actions', profile.get('parameters')) or {},
                 'routes': profile.get('routes') or {},
                 'checkpoint': dependency.get('checkpoint_step_id', correction.get('checkpoint_step_id')),
                 'stop_after_step': dependency.get('stop_after_step', profile.get('stop_after_step')),
                 'conditions': archive.get('snapshot', {}).get('conditions'), 'seed': archive.get('seed')}
    return hashlib.sha256(json.dumps(canonical(signature), sort_keys=True, allow_nan=False).encode()).hexdigest()
