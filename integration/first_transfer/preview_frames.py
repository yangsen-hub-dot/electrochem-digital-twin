"""Bounded, best-effort preview packets, independent of the authoritative MP4.

Copied into first_transfer only by the explicitly staged overlay builder.
The render thread submits a copied image plus a confirmed ledger snapshot;
JPEG/base64/disk I/O run in one worker. No chemistry, error draws or receipts.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import threading
import time

MAX_PACKET_BYTES = 4 * 1024 * 1024


def synchronized_state(snapshot, *, frame_index, simulation_time_s, phase, command=None):
    """Only confirmed inventory/voltage; an in-flight command is a label, not a result."""
    state = snapshot.get('state', {})
    cells = {}
    for side in ('left', 'right'):
        liquid = state.get('containers', {}).get(side + '_half_cell', {})
        cells[side] = {key: deepcopy(liquid.get(key)) for key in
                       ('volume_ul', 'concentrations_m', 'mixing_uniformity')}
    voltage = snapshot.get('voltage', {})
    valid = voltage.get('valid') is True and not snapshot.get('state_uncertain', False)
    return {
        'frame_index': frame_index, 'simulation_time_s': simulation_time_s,
        'phase': str(phase), 'step_id': (command or {}).get('step_id'),
        'action': (command or {}).get('action'),
        'state_revision': snapshot.get('state_revision'), 'last_seq': snapshot.get('last_seq'),
        'run_status': snapshot.get('status'), 'state_uncertain': bool(snapshot.get('state_uncertain')),
        'completed_steps': snapshot.get('completed_steps'), 'total_steps': snapshot.get('total_steps'),
        'cells': cells, 'temperature_k': state.get('temperature_k'),
        'predicted_cell_mv': voltage.get('predicted_cell_mv') if valid else None,
        'virtual_meter_mv': voltage.get('virtual_meter_mv') if valid else None,
        'voltage_valid': valid, 'data_origin': 'simulated',
        'quantity_basis': 'last_successful_receipt_at_frame_capture',
        'in_flight_visuals_are_not_committed_inventory': True,
    }


class PreviewPublisher:
    def __init__(self, run_dir, run_id, *, interval_s=.5, max_size=(1600, 900),
                 quality=82, clock=time.monotonic):
        if not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise ValueError('Invalid preview run id')
        if not math.isfinite(interval_s) or interval_s < .1:
            raise ValueError('Preview interval must be >= 0.1 seconds')
        self.root = Path(run_dir) / 'live_preview'
        self.root.mkdir(exist_ok=True)
        self.path = self.root / 'latest.json'
        if self.path.exists():
            raise ValueError('Never reuse preview packets from an old run')
        self.run_id, self.interval_s, self.clock = run_id, interval_s, clock
        self.max_size, self.quality = max_size, quality
        self._condition = threading.Condition()
        self._slot = None
        self._closed = False
        self._terminal = 'ended'
        self._sequence = 0
        self._next_due = -math.inf
        self._last_packet = None
        self.last_error = None
        self.dropped_frames = 0
        self._thread = threading.Thread(target=self._work, name='preview-publisher', daemon=True)
        self._thread.start()

    def due(self):
        return not self._closed and self.clock() >= self._next_due

    def publish(self, image, snapshot, *, frame_index, simulation_time_s, phase, command=None):
        if snapshot.get('run_id') != self.run_id:
            raise ValueError('Frame and confirmed state belong to different runs')
        if not self.due():
            return False
        state = synchronized_state(snapshot, frame_index=frame_index,
                                   simulation_time_s=simulation_time_s, phase=phase, command=command)
        # Copy before queuing; a renderer may reuse its image buffers immediately.
        item = (image.copy(), state, datetime.now(timezone.utc).isoformat())
        with self._condition:
            if self._closed:
                return False
            if self._slot is not None:
                self.dropped_frames += 1
            self._slot = item
            self._next_due = self.clock() + self.interval_s
            self._condition.notify()
        return True

    def _write(self, packet):
        data = json.dumps(packet, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(data) > MAX_PACKET_BYTES:
            raise ValueError('Preview packet too large')
        temporary = self.path.with_suffix('.tmp')
        try:
            temporary.write_bytes(data)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _encode(self, image):
        image = image.convert('RGB')
        image.thumbnail(self.max_size)
        stream = BytesIO()
        image.save(stream, format='JPEG', quality=self.quality)
        return image.width, image.height, base64.b64encode(stream.getvalue()).decode('ascii')

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._slot is not None or self._closed)
                item, self._slot = self._slot, None
                if item is None and self._closed:
                    break
            try:
                image, state, captured = item
                width, height, jpeg = self._encode(image)
                self._sequence += 1
                packet = {'schema_version': '1.0', 'run_id': self.run_id,
                          'sequence': self._sequence, 'captured_at_utc': captured,
                          'stream_status': 'live', 'frame_state': state,
                          'width': width, 'height': height, 'image_mime': 'image/jpeg',
                          'image_base64': jpeg, 'dropped_preview_frames': self.dropped_frames}
                self._write(packet)
                self._last_packet = packet
            except Exception as exc:
                self.last_error = type(exc).__name__  # Preview failure never fails the experiment.
        if self._last_packet:
            try:
                self._write({**self._last_packet, 'stream_status': self._terminal})
            except Exception as exc:
                self.last_error = type(exc).__name__

    def close(self, status='ended', timeout=2):
        if status not in {'ended', 'failed'}:
            raise ValueError('Invalid terminal preview status')
        with self._condition:
            self._terminal, self._closed = status, True
            self._condition.notify()
        self._thread.join(timeout)
        return not self._thread.is_alive()


def optional_publisher(run_dir, run_id, *, enabled):
    if not enabled:
        return None
    try:
        return PreviewPublisher(run_dir, run_id)
    except Exception:
        # No preview must not prevent the original recording path from working.
        return None
