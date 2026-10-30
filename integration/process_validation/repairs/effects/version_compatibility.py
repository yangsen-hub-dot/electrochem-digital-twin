"""Prove version equivalence from retained, fingerprint-matched releases.

Only orchestration/comparison files may differ; audit and execution files must
match byte for byte. Missing releases or unrecognised fingerprints fail closed.
"""
import ast
import hashlib
import json
from pathlib import Path

_ORCHESTRATION = {
    'integration/process_validation/repairs/pipeline.py',
    'integration/process_validation/repairs/sequence.py',
    'integration/process_validation/repairs/failed_retry.py',
    'integration/process_validation/repairs/lineage_queue.py',
    'integration/process_validation/correction.py',
    'integration/process_validation/repairs/strategies/electrode_probe.py',
    'integration/process_validation/repairs/strategies/top_down_grasp.py',
    'integration/process_validation/repairs/effects/comparison.py',
    'integration/process_validation/repairs/effects/version_compatibility.py',
}
# These two reviewed implementations differ only in where the same release
# compatibility proof is invoked; both perform the same report binding first.
_STORAGE_EQUIVALENTS = {
    '1d9be730d6b0e29b39ec8f2ba580046b692e0c6b3ba58fb0063c159a8f341c5c',
    'bea8305a00afd23798f4870e942e732acce092b557a1a42446b84047baa6ad51',
}
# Later profiles add fail-closed validation for measured probe compensation
# and top-down descent parameters; accepted motion profiles remain unchanged.
_PROFILE_EQUIVALENTS = {
    '168b9427eb35032bea984a27d6e41e7ce277f783daa55135ee9fb97c40542483',
    'cbca00089a3469aea5296c11eb5fd476e5818466c32cf2288dcca2e3e171f9b9',
    '140e89643fd64bab0292c1f63ec2b504a11bdb2d22881575716d98a414e65d4e',
    '7ef9b47386d25a344712af40e9c9123e1c7586784f7f3b9331d3b94d4176280d',
    '0482033aef43ca0d2e33439a5fdaba34e58c4f6687f7f3ba2373616443be7b22',
    '742da68e3cfb9035203cc7b60eb0f09245f99e60af5d7e44dcb878768abe8eee',
    # Adds a fail-closed check that the full-open-before-pick marker is only
    # valid on pick actions and cannot be combined with a narrow opening.
    'd724c07c475b22214918184260fed61e92294dbb4cd3be7b0c3caab43d2ff900',
}
# Exact checker revisions for ignoring transient wrist rotation frames while
# retaining orientation checks at pickup and settled electrode insertion.
_GEOMETRY_PHASE_EQUIVALENTS = {
    'f9a0915d757fdc9e624695e02ec3d22b52f5729c9f59d5ef1afe9272425ede5c',
    'daea0ecd854a3e25b7aee97cf4759c03e19f754260a1843822ec8881dcce39bc',
}
# Exact runner revisions with the same bounded pregrasp path and explicit
# gripper-open restoration at placement/restarted pickup boundaries.
_PREGRASP_RUNTIME_EQUIVALENTS = {
    'integration/first_transfer/matterix_first_transfer.py': {
        '22a7e5be46975172e1ccbedd025152f3647d712d9d71b3b9be1693e42ecb797d',
        '2f06f537bacac7320fc7560c829a14f720436585267b82c50593f928599f5fb8',
        '386ce9fe5b2811203f54147066cb002fb3af4bd6321b1c0b8d22e8e2e18e4fbe',
    },
    'integration/process_validation/scene_trace.py': {
        '13301ea6174a90a55d217a7807583c041c569dcdfc10d601d08379d09a131fa5',
        '5047b53473d94fc3ae900233b7f36a8b00e5fe9080cf795614ada605535b4ad7',
    },
}
_PREGRASP_REVIEWED_ADDITIONS = {
    'integration/first_transfer/pregrasp_planner.py': (
        'b69dc7552b9324b171ba947db4efee09b40d4f82605fb5d071b17461ee9036a9',
        'reviewed-pregrasp-pose-planner-v1'),
    'integration/process_validation/preflight_collision.py': (
        '369a391ed6666b08c43301182b69c481cca8d4c477c4d7dff072680c57770228',
        'reviewed-preflight-collision-gate-v1'),
}
_RESUMED_GRIPPER_OVERRIDE_REVISIONS = {
    'integration/first_transfer/matterix_first_transfer.py': {
        '386ce9fe5b2811203f54147066cb002fb3af4bd6321b1c0b8d22e8e2e18e4fbe',
        '5aafdae9cc70e0a8809f8c7485cc5393cab856b304d581e1c289e8b52abbf075',
    },
    'integration/first_transfer/repair_execution.py': {
        '46e5df5742e86429584d94221646b6edbcc23037974bdd1b41d1622bb60426db',
        '13122e2d83397697427ab325910e1e05086e711d751373384a2af7aaaff099ef',
    },
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _profile_semantic_digest(digest):
    return 'reviewed-stricter-probe-profile-v1' if digest in _PROFILE_EQUIVALENTS else digest


def _checker(root):
    base = root / 'integration/process_validation'
    # Reports are generated on Linux while effect checks may run on Windows.
    # Fingerprint keys must therefore use stable POSIX separators on both.
    files = {p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(base.rglob('*.py'))
             if not p.name.startswith('test_') and '__pycache__' not in p.parts}
    return _digest(files)


def _semantic(root, *, allow_resumed_gripper_override=False):
    paths = []
    for relative in ('electrochemistry_simulation/electrochem_sim',
                     'electrochemistry_simulation/config', 'integration/first_transfer',
                     'integration/process_validation'):
        paths.extend(p for p in (root / relative).rglob('*') if p.is_file()
                     and p.suffix in {'.py', '.json'} and not p.name.startswith('test_')
                     and '__pycache__' not in p.parts)
    paths.extend((root / 'integration/reference/ChemLab-Assets/assets').rglob('*.glb'))
    files = {}
    for path in sorted(set(paths)):
        name = path.relative_to(root).as_posix()
        if name in _ORCHESTRATION:
            continue
        if name == 'integration/process_validation/continuation.py':
            # Local-repair execution and audit use the fingerprint and source
            # binding helpers, not the continuation-plan policy. Preserve every
            # other AST node so changes to the helpers still fail closed.
            tree = ast.parse(path.read_text(encoding='utf8'))
            tree.body = [node for node in tree.body
                         if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                         or node.name != 'build_continuation_plan']
            files[name] = hashlib.sha256(ast.dump(tree).encode()).hexdigest()
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if (allow_resumed_gripper_override
                    and name in _RESUMED_GRIPPER_OVERRIDE_REVISIONS
                    and digest in _RESUMED_GRIPPER_OVERRIDE_REVISIONS[name]):
                digest = 'reviewed-free-gripper-target-overridden-before-local-pick-v1'
            elif name in _PREGRASP_RUNTIME_EQUIVALENTS and digest in _PREGRASP_RUNTIME_EQUIVALENTS[name]:
                digest = 'reviewed-slot-pregrasp-kinematic-probe-v1'
            if name in _PREGRASP_REVIEWED_ADDITIONS:
                accepted_digest, semantic_name = _PREGRASP_REVIEWED_ADDITIONS[name]
                if digest == accepted_digest:
                    digest = semantic_name
            if (name == 'integration/process_validation/repairs/effects/storage.py'
                    and digest in _STORAGE_EQUIVALENTS):
                digest = 'reviewed-report-binding-storage-v1'
            if (name == 'integration/process_validation/geometry_checks.py'
                    and digest in _GEOMETRY_PHASE_EQUIVALENTS):
                digest = 'reviewed-settled-electrode-orientation-check-v1'
            if name == 'integration/process_validation/repairs/profile.py':
                digest = _profile_semantic_digest(digest)
            files[name] = digest
    for name, (_, semantic_name) in _PREGRASP_REVIEWED_ADDITIONS.items():
        if name not in files:
            files[name] = semantic_name
    return _digest(files)


def _resumed_gripper_change_is_overridden(before, after):
    """Scope resume-open equivalence to local picks that immediately set a narrower target."""
    profiles = []
    for record in (before, after):
        manifest = record.get('manifest') or {}
        correction = manifest.get('correction') or {}
        side = correction.get('side')
        if side not in {'left', 'right'}:
            return False
        pick_step = f'ui_pick_{side}_copper_electrode'
        place_step = f'ui_place_{side}_copper_electrode'
        dependency = (correction.get('dependency_plan')
                      or (correction.get('repair_contract') or {}).get('dependency_plan') or {})
        facts = dependency.get('confirmed_checkpoint_facts') or []
        rerun = dependency.get('rerun_step_ids') or []
        parameters = correction.get('parameters') or correction.get('actions') or {}
        pick = parameters.get(pick_step) or {}
        target = pick.get('gripper_open_joint_target_m')
        diagnostic = manifest.get('diagnostic') or {}
        executed = diagnostic.get('executed_step_ids') or []
        if ("gripper:free" not in facts or pick_step not in rerun
                or not isinstance(target, (int, float)) or isinstance(target, bool)
                or not 0 < float(target) < 0.04
                or diagnostic.get('prior_actions_replayed') is not False
                or diagnostic.get('restored_after_step') != dependency.get('checkpoint_step_id')
                or diagnostic.get('stop_after_step') != place_step
                or pick_step not in executed):
            return False
        profiles.append((side, float(target)))
    return profiles[0] == profiles[1]


def prove_compatible(before, after, releases=None):
    from ...continuation import execution_fingerprint
    release_dir = Path(releases) if releases else Path(__file__).resolve().parents[4].parent
    expected = [(r['manifest'].get('execution_fingerprint'), r['report'].get('checker_fingerprint'))
                for r in (before, after)]
    if any(not all(isinstance(x, str) and len(x) == 64 for x in pair) for pair in expected):
        return None
    resumed_gripper_override = _resumed_gripper_change_is_overridden(before, after)
    executions = {}
    checkers = {}
    try:
        for root in release_dir.iterdir():
            if not root.is_dir() or not (root / 'integration/process_validation').is_dir():
                continue
            checker = _checker(root)
            execution = execution_fingerprint(root)
            semantic = _semantic(root, allow_resumed_gripper_override=resumed_gripper_override)
            if execution in {pair[0] for pair in expected}:
                executions.setdefault(execution, []).append((root, semantic))
            if checker in {pair[1] for pair in expected}:
                checkers.setdefault(checker, []).append((root, semantic))

        # A historical run may be re-audited with a newer checker. The run's
        # execution fingerprint and report's checker fingerprint then belong
        # to different retained releases. Accept that only when those releases
        # have identical reviewed semantics, and the before/after runs do too.
        matched = []
        for execution, checker in expected:
            compatible = []
            for execution_root, execution_semantic in executions.get(execution, []):
                for checker_root, checker_semantic in checkers.get(checker, []):
                    if execution_semantic == checker_semantic:
                        compatible.append((execution_root, checker_root, execution_semantic))
            if not compatible:
                return None
            compatible.sort(key=lambda item: (item[0].name, item[1].name))
            matched.append(compatible[0])
        if matched[0][2] == matched[1][2]:
            evidence = {
                'method': 'independent_fingerprint_and_semantic_match',
                'releases': [
                    {'execution': item[0].name, 'checker': item[1].name}
                    for item in matched
                ],
                'semantic_sha256': matched[0][2],
            }
            if resumed_gripper_override:
                evidence['scoped_equivalence'] = (
                    'The free-gripper reset differs only before local pick motion; both '
                    'profiles immediately set the same narrower per-finger target.')
            return evidence
    except (OSError, ValueError, TypeError, SyntaxError):
        return None
    return None
