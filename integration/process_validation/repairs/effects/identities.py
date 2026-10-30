"""Compare distinct problems, not counts of frames or duplicated pause events."""
import json

from ..evidence import report_rows


def bound_step(row, archive):
    if row.get('step_id'): return row['step_id']
    if row.get('code') != 'execution_failed': return None
    matches = {event.get('step_id') for event in archive.get('events', [])
               if event.get('type') == 'step.failed'
               and (event.get('payload', {}).get('reason') or
                    event.get('payload', {}).get('executor_receipt', {}).get('reason')) == row.get('message')}
    return next(iter(matches)) if len(matches) == 1 else None


def problem_key(row, archive):
    phase = row.get('phase')
    if row.get('code') == 'execution_failed' and not phase:
        message = row.get('message', '')
        try: phase = json.JSONDecoder().raw_decode(message[message.index('{'):])[0].get('phase')
        except (ValueError, TypeError, AttributeError): pass
    return tuple(row.get(key) for key in ('group', 'code')) + (bound_step(row, archive),) + tuple(
        row.get(key) for key in ('body', 'obstacle', 'container', 'species', 'field')) + (phase,)


def failures(report, archive):
    return {problem_key(row, archive): row for row in report_rows(report)
            if row['status'] == 'fail' and (row['group'], row['code']) != ('records', 'scope')}


def describe(keys, rows):
    return [dict(group=key[0], code=key[1], step_id=key[2], body=key[3], obstacle=key[4],
                 container=key[5], species=key[6], field=key[7], phase=key[8],
                 message=rows[key].get('message')) for key in sorted(keys, key=repr)]
