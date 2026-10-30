"""GPU routing option checks, independent of installed Isaac/GPU hardware."""
import os
import sys
import types
import tempfile
import unittest
from unittest.mock import patch
from simulator_runtime import selected_gpu_id, launch_headless_simulator, isolate_runtime_recorders


class RuntimeDeviceTests(unittest.TestCase):
    def test_upstream_recorders_never_share_the_default_hdf5_path(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            configs = [types.SimpleNamespace(recorders=types.SimpleNamespace()) for _ in range(2)]
            for config, name in zip(configs, ('run_a','run_b')):
                isolate_runtime_recorders(config, Path(root) / name)
                self.assertTrue(Path(config.recorders.dataset_export_dir_path).is_dir())
                self.assertEqual(config.recorders.dataset_filename, name)
                self.assertEqual(Path(config.record_path), Path(config.recorders.dataset_export_dir_path) / (name + '.hdf5'))
            self.assertNotEqual(configs[0].recorders.dataset_export_dir_path, configs[1].recorders.dataset_export_dir_path)

    def test_explicit_batch_device_selects_cuda_and_disables_multi_gpu(self):
        module = types.ModuleType('isaaclab.app')
        captured = {}
        class Launcher:
            def __init__(self, **options):
                captured.update(options)
                self._sim_app_config = {}
                self._create_app()
                self.app = object()
            def _create_app(self):
                captured.update(self._sim_app_config)
        module.AppLauncher = Launcher
        usd = types.ModuleType('omni.usd')
        usd.get_context = lambda: types.SimpleNamespace(get_stage=lambda: object())
        omni = types.ModuleType('omni')
        omni.usd = usd
        with patch.dict(sys.modules, {'isaaclab.app': module,'omni':omni,'omni.usd':usd}), patch.dict(os.environ, {'ELECTROCHEM_GPU_ID':'1','ELECTROCHEM_CPU_THREADS':'4'}), patch('simulator_runtime.version', return_value='6.0.1'):
            self.assertIsNotNone(launch_headless_simulator())
            self.assertEqual(captured['device'], 'cuda:1')
            self.assertFalse(captured['multi_gpu'])
            self.assertEqual(captured['limit_cpu_threads'], 4)
            self.assertFalse(captured['create_new_stage'])

    def test_bad_device_is_rejected(self):
        for value in ('-1','cuda:0','bad'):
            with patch.dict(os.environ, {'ELECTROCHEM_GPU_ID':value}), self.assertRaises(ValueError):
                selected_gpu_id()
