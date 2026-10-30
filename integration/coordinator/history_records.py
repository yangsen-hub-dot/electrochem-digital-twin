"""Read-only, reaction-neutral normalization of persisted cloud run archives."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re

RUN_ID = re.compile(r'[a-f0-9]{32}')


def summarize_archive(document, run_id, metadata=None):
    metadata = metadata or {}
    config = document.get('config', {})
    snapshot = document.get('snapshot')
    numerical = snapshot is None
    if numerical:
        state = document.get('final_state', {})
        prediction = document.get('prediction', {})
        status = document.get('status', 'unknown')
        completed = state.get('completed_steps', 0)
        total = len(config.get('protocol', []))
        conditions = config.get('condition_inputs', {})
        realized = document.get('error_model_realized', {})
        conservation = document.get('conservation', {})
        fully_completed = status == 'completed'
        measurement_uncertainty = document.get('measurement_uncertainty', {})
    else:
        state = snapshot.get('state', {})
        voltage = snapshot.get('voltage', {})
        prediction = voltage.get('prediction', {})
        status = snapshot.get('status', 'unknown')
        completed, total = snapshot.get('completed_steps', 0), snapshot.get('total_steps', 0)
        conditions = snapshot.get('conditions', {})
        realized = snapshot.get('error_model_realized', {})
        conservation = snapshot.get('conservation', {})
        fully_completed = bool(snapshot.get('full_experiment_completed'))
        measurement_uncertainty = voltage.get('measurement_uncertainty', {})
    cell = config.get('cell', {})
    if metadata.get('status') in {'failed','paused','awaiting_validation'}:
        status = metadata['status']
    species = cell.get('active_species') or cell.get('redox_reaction', {}).get('display_species')
    containers = state.get('containers', {})
    left = containers.get(cell.get('left_container', 'left_half_cell'), {})
    right = containers.get(cell.get('right_container', 'right_half_cell'), {})
    components = prediction.get('voltage_components_mv', {})
    activity = prediction.get('activity_correction', {})
    junction = prediction.get('liquid_junction_potential', {})
    comparison = junction.get('model_comparison', {})
    valid = bool(fully_completed and status == 'completed' and prediction.get('valid'))
    def volume(liquid):
        return liquid.get('volume_l', 0) * 1000 if 'volume_l' in liquid else (None if liquid.get('volume_ul') is None else liquid['volume_ul'] / 1000)
    created = metadata.get('created_at_utc') or document.get('created_at_utc')
    model = conditions.get('liquid_junction_model', junction.get('model'))
    row = {
        'run_id': run_id, 'file': run_id + ('/result.json' if numerical else '/run.json'),
        'created_at_utc': created, 'experiment_id': config.get('experiment_id'),
        'experiment_name': config.get('experiment_name_short_zh') or config.get('experiment_name_zh') or config.get('experiment_id', '未命名实验'),
        'redox_couple': cell.get('redox_reaction', {}).get('name_zh') or species,
        'active_species_name': config.get('species', {}).get(species, {}).get('name', species),
        'template_id': metadata.get('template_id', 'copper_concentration_cell' if species == 'Cu' else None),
        'execution_mode': metadata.get('execution_mode', 'numerical' if numerical else 'workflow_video'),
        'record_kind': 'numerical' if numerical else ('continued_workflow' if metadata.get('continuation')
                        else 'complete_workflow' if fully_completed else 'partial_workflow'),
        'source_run_id': metadata.get('source_run_id'),
        'root_run_id': (metadata.get('continuation') or {}).get('root_run_id'),
        'video_scope': (snapshot or {}).get('video', {}).get('recording_scope') if not numerical else None,
        'status': status, 'valid': valid, 'prediction_valid': valid,
        'full_experiment_completed': fully_completed if not numerical else False,
        'data_origin': 'simulated', 'measured_mv': None,
        'batch_id': metadata.get('batch_id'), 'matrix_index': metadata.get('matrix_index'),
        'replicate': metadata.get('replicate'), 'replicate_count': metadata.get('replicate_count'),
        'seed': document.get('seed', metadata.get('seed')),
        'conditions': deepcopy(conditions), 'completed_steps': completed, 'total_steps': total,
        'c_left_m': left.get('concentrations_m', {}).get(species),
        'c_right_m': right.get('concentrations_m', {}).get(species),
        'target_c_left_m': conditions.get('left_concentration_m'),
        'target_c_right_m': conditions.get('right_concentration_m'),
        'left_volume_ml': volume(left), 'right_volume_ml': volume(right),
        'target_left_volume_ml': conditions.get('left_volume_ml'),
        'target_right_volume_ml': conditions.get('right_volume_ml'),
        'temperature_c': conditions.get('temperature_c'),
        'salt_bridge_type': conditions.get('salt_bridge_type'),
        'salt_bridge_concentration_m': conditions.get('salt_bridge_concentration_m'),
        'mixing_duration_s': conditions.get('mixing_duration_s', 10.0),
        'voltage_mv': prediction.get('voltage_mv') if valid else None,
        'measurement_uncertainty_model': measurement_uncertainty.get('model_id'),
        'measurement_uncertainty_lower_mv': measurement_uncertainty.get('lower_bound_mv') if valid else None,
        'measurement_uncertainty_upper_mv': measurement_uncertainty.get('upper_bound_mv') if valid else None,
        'measurement_uncertainty_origin': measurement_uncertainty.get('data_origin'),
        'ideal_nernst_mv': components.get('ideal_nernst'),
        'activity_correction_mv': components.get('activity_correction'), 'activity_status': activity.get('status'),
        'activity_enabled': activity.get('enabled', conditions.get('activity_correction_enabled', False)),
        'liquid_junction_enabled': junction.get('enabled', conditions.get('liquid_junction_enabled', False)),
        'liquid_junction_mv': components.get('liquid_junction_potential'),
        'liquid_junction_status': junction.get('status'), 'liquid_junction_model': model,
        'liquid_junction_model_name_zh': 'Henderson 近似' if model == 'henderson_approximation' else '简化 N–P',
        'liquid_junction_henderson_mv': comparison.get('henderson_approximation', {}).get('correction_mv'),
        'liquid_junction_nernst_planck_mv': comparison.get('nernst_planck_zero_current_linear_profile', {}).get('correction_mv'),
        'liquid_junction_model_difference_mv': comparison.get('difference_nernst_planck_minus_henderson_mv'),
        'errors_enabled': realized.get('enabled', conditions.get('errors_enabled', False)),
        'error_levels': conditions.get('operation_error_levels', {}),
        'conservation_passed': bool(conservation.get('passed')), 'failure': document.get('failure') or metadata.get('failure'),
        'local_voltage_range_mv': prediction.get('local_voltage_range_mv'),
        'video_available': bool(not numerical and snapshot.get('video', {}).get('status') == 'ready'),
    }
    return row


class CloudHistory:
    def __init__(self, runs_root, jobs_root, model_options):
        self.runs_root, self.jobs_root = runs_root, jobs_root
        self.model_options = model_options
        self.cache = {}

    def records(self):
        rows = []
        # Cache by file size and mtime, never overwrite or reinterpret old archives.
        for folder in self.runs_root.iterdir():
            if not folder.is_dir() or not RUN_ID.fullmatch(folder.name):
                continue
            path = folder / 'result.json'
            if not path.is_file():
                path = folder / 'run.json'
            if not path.is_file():
                continue
            metadata_path = folder / 'batch_metadata.json'
            if not metadata_path.is_file():
                metadata_path = self.runs_root / (folder.name + '.metadata.json')
            job_path = self.jobs_root / (folder.name + '.job.json')
            try:
                signature = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.is_file() else None
                                  for p in (path, metadata_path, job_path))
                cached = self.cache.get(folder.name)
                if cached and cached[0] == signature:
                    rows.append(deepcopy(cached[1]))
                    continue
                document = json.loads(path.read_text(encoding='utf-8'))
                recorded_id = document.get('run_id') or document.get('snapshot', {}).get('run_id')
                if recorded_id != folder.name:
                    continue
                metadata = json.loads(job_path.read_text(encoding='utf-8')) if job_path.is_file() else {}
                if metadata_path.is_file():
                    metadata.update(json.loads(metadata_path.read_text(encoding='utf-8')))
                metadata.setdefault('created_at_utc', datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
                row = summarize_archive(document, folder.name, metadata)
                self.cache[folder.name] = (signature, row)
                rows.append(deepcopy(row))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        known = {row['run_id'] for row in rows}
        for path in self.runs_root.glob('*.metadata.json'):
            run_id = path.name.split('.')[0]
            if run_id in known or not RUN_ID.fullmatch(run_id):
                continue
            try:
                metadata = json.loads(path.read_text(encoding='utf-8'))
                if metadata.get('status') != 'failed':
                    continue
                row = summarize_archive({'config': {'condition_inputs': metadata.get('conditions', {}),
                    'experiment_id': metadata.get('template_id')}, 'status': 'failed',
                    'failure': metadata.get('failure')}, run_id, metadata)
                row['record_kind'] = 'failed_before_archive'
                rows.append(row)
            except (OSError, ValueError, TypeError):
                continue
        return sorted(rows, key=lambda row: row.get('created_at_utc') or '', reverse=True)

    def payload(self):
        records = self.records()
        return {'schema_version': '1.0', 'count': len(records), 'records': records,
                'liquid_junction_model_options': self.model_options, 'data_origin': 'simulated', 'read_only': True}


def sweep_row(record):
    row = deepcopy(record)
    row.update({'scenario': (str(record.get('matrix_index') or record['run_id'][:8])),
                'run_status': record['status'], 'scenario_status': record['status'],
                'temperature_k': (record.get('temperature_c') or 0) + 273.15,
                'actual_c_left_m': record.get('c_left_m'), 'actual_c_right_m': record.get('c_right_m'),
                'final_theoretical_voltage_mv': record.get('voltage_mv'),
                'activity_correction_enabled': record.get('activity_enabled'),
                'liquid_junction_correction_mv': record.get('liquid_junction_mv')})
    for factor in ('pipetting', 'tip_carryover', 'mixing'):
        row[factor + '_level'] = record['error_levels'].get(factor)
    return row
