"""CPU-only synthetic-image tests. No engine, renderer, real cloud or browser."""
import base64
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from preview_frames import PreviewPublisher, optional_publisher, synchronized_state
from preview_reader import preview_payload, MAX_PACKET_BYTES
from prepare_overlay import prepare, WEB, SERVER, RUNNER

SOURCE = Path(__file__).resolve().parents[2]
RUN = 'a' * 32


def snapshot(revision=1):
    return {'run_id': RUN, 'status':'running', 'state_revision':revision,
            'last_seq':revision * 2, 'completed_steps':revision, 'total_steps':32,
            'state': {'temperature_k':298.15, 'containers': {
                'left_half_cell': {'volume_ul':revision * 100, 'concentrations_m':{'Cu':.01}, 'mixing_uniformity':.9}}},
            'voltage': {'valid':False, 'virtual_meter_mv':99}, 'state_uncertain':False}


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / RUN
        self.run.mkdir()
        self.clock = [0.]
        self.publishers = []

    def tearDown(self):
        for publisher in self.publishers:
            publisher.close()
        self.temp.cleanup()

    def publisher(self, **kw):
        p = PreviewPublisher(self.run, RUN, clock=lambda:self.clock[0], **kw)
        self.publishers.append(p)
        return p

    def publish(self, p, revision=1):
        return p.publish(Image.new('RGB',(80,60),(revision,0,0)), snapshot(revision),
                         frame_index=revision, simulation_time_s=revision / 12, phase='dispense/in_progress')

    def test_frame_and_confirmed_data_bound_without_mutation(self):
        p = self.publisher()
        state = snapshot()
        original = json.dumps(state)
        p.publish(Image.new('RGB',(80,60),'blue'), state, frame_index=15, simulation_time_s=1.25, phase='aspirate')
        state['state']['containers']['left_half_cell']['volume_ul'] = 999
        self.assertTrue(p.close())
        packet = preview_payload(self.root, RUN)
        self.assertEqual(packet['frame_state']['frame_index'],15)
        self.assertEqual(packet['frame_state']['cells']['left']['volume_ul'],100)
        self.assertIsNone(packet['frame_state']['virtual_meter_mv'])
        self.assertEqual(packet['stream_status'],'ended')
        self.assertIn('"volume_ul": 100', original)

    def test_invalid_or_uncertain_voltage_not_published_as_valid(self):
        state = snapshot(); state['voltage']['valid'] = True
        state['state_uncertain'] = True
        result = synchronized_state(state, frame_index=1, simulation_time_s=1, phase='paused')
        self.assertFalse(result['voltage_valid'])
        self.assertIsNone(result['virtual_meter_mv'])

    def test_inflight_command_is_only_a_label(self):
        result = synchronized_state(snapshot(), frame_index=1, simulation_time_s=1,
                                    phase='dispense', command={'step_id':'s2','action':'dispense','actual_volume_ul':888})
        self.assertEqual(result['cells']['left']['volume_ul'],100)
        self.assertEqual(result['step_id'],'s2')

    def test_throttled_preview_never_touches_full_mp4(self):
        mp4 = self.run / 'first_transfer.mp4'; mp4.write_bytes(b'full-recording-sentinel')
        p = self.publisher(); self.assertTrue(self.publish(p)); self.assertFalse(self.publish(p,2))
        self.clock[0] = .51; self.assertTrue(self.publish(p,3)); p.close()
        self.assertEqual(mp4.read_bytes(),b'full-recording-sentinel')
        self.assertEqual(preview_payload(self.root,RUN)['frame_state']['state_revision'],3)

    def test_slow_encoder_has_one_pending_slot_not_unbounded_queue(self):
        p = self.publisher(); entered, release = threading.Event(), threading.Event()
        encode = p._encode
        def slow(image):
            entered.set(); release.wait(2); return encode(image)
        p._encode = slow
        self.publish(p); self.assertTrue(entered.wait(1))
        for revision in range(2,11):
            self.clock[0] += .51; self.publish(p,revision)
        self.assertEqual(p.dropped_frames,8)
        release.set(); self.assertTrue(p.close())
        self.assertEqual(preview_payload(self.root,RUN)['frame_state']['state_revision'],10)

    def test_failed_encoder_is_best_effort(self):
        p = self.publisher()
        def fail(_): raise RuntimeError('synthetic encoding failure')
        p._encode = fail
        self.publish(p); self.assertTrue(p.close())
        self.assertEqual(p.last_error,'RuntimeError')
        self.assertFalse(preview_payload(self.root,RUN)['available'])

    def test_wrong_run_rejected(self):
        p = self.publisher(); state = snapshot(); state['run_id'] = 'b'*32
        with self.assertRaises(ValueError):
            p.publish(Image.new('RGB',(8,8)),state,frame_index=0,simulation_time_s=0,phase='x')

    def test_no_frame_and_run_ids(self):
        self.assertFalse(preview_payload(self.root,RUN)['available'])
        for run_id in ('../secret',None,'b'*31):
            with self.assertRaises(ValueError): preview_payload(self.root,run_id)
        with self.assertRaises(ValueError): preview_payload(self.root,RUN,-1)

    def test_unchanged_terminal_and_stale_still_reported(self):
        p = self.publisher(); self.publish(p); p.close('failed')
        first = preview_payload(self.root,RUN)
        later = datetime.now(timezone.utc)+timedelta(seconds=12)
        packet = preview_payload(self.root,RUN,first['sequence'],now=later)
        self.assertTrue(packet['unchanged']); self.assertTrue(packet['stale'])
        self.assertEqual(packet['stream_status'],'failed'); self.assertNotIn('image_base64',packet)

    def test_jpeg_resolution_limited(self):
        p = self.publisher(max_size=(160,90))
        p.publish(Image.new('RGB',(1000,1000)),snapshot(),frame_index=0,simulation_time_s=0,phase='x'); p.close()
        packet = preview_payload(self.root,RUN)
        self.assertLessEqual(packet['width'],160); self.assertLessEqual(packet['height'],90)
        self.assertTrue(base64.b64decode(packet['image_base64']).startswith(b'\xff\xd8'))

    def test_damaged_packet_and_size_limit(self):
        folder = self.run / 'live_preview'; folder.mkdir()
        target = folder / 'latest.json'; target.write_text('{bad',encoding='utf8')
        with self.assertRaises(ValueError): preview_payload(self.root,RUN)
        target.write_bytes(b' '*(MAX_PACKET_BYTES+1))
        with self.assertRaises(ValueError): preview_payload(self.root,RUN)

    def test_old_packets_are_never_reused(self):
        p = self.publisher(); self.publish(p); p.close()
        self.assertIsNone(optional_publisher(self.run,RUN,enabled=True))
        self.assertIsNone(optional_publisher(self.run,RUN,enabled=False))


class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.overlay = cls.root / 'overlay'
        cls.manifest = prepare(SOURCE,cls.overlay)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_overlay_does_not_install_or_change_sources(self):
        import hashlib
        for name, digest in self.manifest['original_sha256'].items():
            self.assertEqual(hashlib.sha256((SOURCE/name).read_bytes()).hexdigest(),digest)
        self.assertTrue(self.manifest['preserved_originals'])
        self.assertFalse(self.manifest['deployed'])
        self.assertIn('writer.append_data', (self.overlay/RUNNER).read_text('utf8'))
        self.assertIn('os.replace(partial, output)', (self.overlay/RUNNER).read_text('utf8'))

    def test_prevent_output_into_live_project(self):
        with self.assertRaises(ValueError): prepare(SOURCE,SOURCE/'integration/live_preview/install-forbidden')

    def test_staged_endpoint_auth_and_cors_without_engine_or_simulation(self):
        spec = importlib.util.spec_from_file_location('preview_staged_server',self.overlay/SERVER)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        class Coordinator: runs_root = self.root / 'runs'
        class Batch:
            def capabilities(self): return {'batch':True}
        Coordinator.runs_root.mkdir(exist_ok=True)
        server = module.create_server('127.0.0.1',0,Coordinator(),'test-token-not-production',Batch())
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        headers = {'Authorization':'Bearer test-token-not-production','Origin':'null'}
        try:
            with self.assertRaises(HTTPError) as error: urlopen(address+'/preview?run_id='+RUN)
            self.assertEqual(error.exception.code,401)
            with urlopen(Request(address+'/preview?run_id='+RUN,headers=headers)) as response:
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                self.assertEqual(response.headers['Access-Control-Allow-Origin'],'null')
                self.assertFalse(json.load(response)['available'])
            with urlopen(Request(address+'/capabilities',headers=headers)) as response:
                self.assertTrue(json.load(response)['live_preview']['available'])
            run_dir = Coordinator.runs_root/RUN; run_dir.mkdir()
            publisher = PreviewPublisher(run_dir,RUN)
            publisher.publish(Image.new('RGB',(10,10)),snapshot(),frame_index=3,simulation_time_s=.25,phase='test')
            publisher.close()
            with urlopen(Request(address+'/preview?run_id='+RUN,headers=headers)) as response:
                packet = json.load(response)
                self.assertTrue(packet['available'])
                self.assertEqual(packet['frame_state']['cells']['left']['volume_ul'],100)
                self.assertEqual(packet['frame_state']['frame_index'],3)
            with urlopen(Request(address+'/preview?run_id='+RUN+'&after_sequence=1',headers=headers)) as response:
                self.assertNotIn('image_base64',json.load(response))
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(address+'/preview?run_id=../secret',headers=headers))
            self.assertEqual(error.exception.code,422)
            with urlopen(Request(address+'/preview',method='OPTIONS',headers={'Origin':'null'})) as response:
                self.assertEqual(response.status,204)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
