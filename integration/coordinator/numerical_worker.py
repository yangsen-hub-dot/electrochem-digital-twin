"""Isolated CPU workflow calculation. Produces no robot receipts or video."""
import argparse
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine-root', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--seed', type=int, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.engine_root))
    integration_root = Path(__file__).resolve().parents[1]
    first_transfer_root = integration_root / 'first_transfer'
    sys.path.insert(0, str(first_transfer_root))
    from electrochem_sim.simulator import run_experiment
    from voltage_measurement_model import specification_envelope
    config = json.loads(args.config.read_text(encoding='utf-8'))
    result = run_experiment(config, args.seed)
    prediction = result.get('prediction', {})
    if result.get('status') == 'completed' and prediction.get('valid'):
        result['measurement_uncertainty'] = specification_envelope(prediction['voltage_mv'])
    result.update(run_id=args.run_id, execution_mode='numerical',
                  created_at_utc=datetime.now(timezone.utc).isoformat(), video_generated=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    os.replace(temporary, args.output)
    if result['status'] != 'completed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
