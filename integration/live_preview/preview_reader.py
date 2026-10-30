"""Read-only packet API; mounted behind the coordinator's existing Bearer auth."""
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re

MAX_PACKET_BYTES = 4 * 1024 * 1024


def preview_payload(runs_root, run_id, after_sequence=0, *, now=None):
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise ValueError('必须指定有效的运行编号')
    if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
        raise ValueError('预览序号必须为非负整数')
    root = Path(runs_root).resolve()
    path = (root / run_id / 'live_preview' / 'latest.json').resolve()
    if not path.is_relative_to(root):
        raise ValueError('预览路径超出运行目录')
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_PACKET_BYTES + 1)
    except FileNotFoundError:
        return {'available': False, 'run_id': run_id, 'reason': 'waiting_for_first_frame_or_preview_not_enabled'}
    except OSError:
        return {'available': False, 'run_id': run_id, 'reason': 'preview_storage_temporarily_unavailable'}
    if len(raw) > MAX_PACKET_BYTES:
        raise ValueError('预览数据大小异常')
    try:
        packet = json.loads(raw)
        checks = [packet['schema_version'] == '1.0', packet['run_id'] == run_id,
                  type(packet['sequence']) is int and packet['sequence'] > 0,
                  packet['stream_status'] in {'live', 'ended', 'failed'},
                  packet['frame_state']['data_origin'] == 'simulated',
                  packet['image_mime'] == 'image/jpeg']
        if not all(checks):
            raise ValueError('Invalid packet metadata')
        image = base64.b64decode(packet['image_base64'], validate=True)
        if not image.startswith(b'\xff\xd8') or not image.endswith(b'\xff\xd9'):
            raise ValueError('Invalid JPEG')
        captured = datetime.fromisoformat(packet['captured_at_utc'])
        if captured.tzinfo is None:
            raise ValueError('Timezone missing')
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError('预览包损坏或运行编号不匹配') from exc
    age = max(0.0, ((now or datetime.now(timezone.utc)) - captured).total_seconds())
    common = {'available': True, 'run_id': run_id, 'sequence': packet['sequence'],
              'stream_status': packet['stream_status'], 'age_s': round(age, 3),
              'stale': age > 10, 'unchanged': packet['sequence'] <= after_sequence}
    # Even unchanged replies carry terminal/stale status, but not another JPEG.
    return common if common['unchanged'] else {**packet, **common}
