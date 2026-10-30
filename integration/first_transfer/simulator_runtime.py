"""Version-aware headless startup; preserves the installed simulator and driver."""
from importlib.metadata import version
import os
from pathlib import Path


def isolate_runtime_recorders(config, run_directory):
    """Keep upstream HDF5 state export separate for every independent run."""
    if getattr(config, 'recorders', None) is not None:
        directory = Path(run_directory) / 'simulator_recording'
        directory.mkdir(parents=True, exist_ok=True)
        config.recorders.dataset_export_dir_path = str(directory)
        config.recorders.dataset_filename = Path(run_directory).name
        # Matterix.setup_recorder() reapplies record_path before manager creation.
        # Keep that higher-level option aligned with the same isolated file.
        config.record_path = str(directory / (Path(run_directory).name + '.hdf5'))


def selected_gpu_id():
    value = os.environ.get('ELECTROCHEM_GPU_ID', '0')
    if not value.isdigit():
        raise ValueError('ELECTROCHEM_GPU_ID must be a non-negative GPU index')
    return int(value)


def launch_headless_simulator():
    from isaaclab.app import AppLauncher

    options = {"headless": True, "enable_cameras": True}
    if 'ELECTROCHEM_GPU_ID' in os.environ:
        options.update(device=f'cuda:{selected_gpu_id()}', multi_gpu=False)
    # Isaac Sim 5.0 and isolated batch launches avoid the default viewport.
    # Create the USD stage explicitly after startup, as verified on this node.
    explicit_stage = version("isaacsim").startswith("5.0.") or 'ELECTROCHEM_GPU_ID' in os.environ
    batch_threads = os.environ.get('ELECTROCHEM_CPU_THREADS')
    if batch_threads is not None and (not batch_threads.isdigit() or not 1 <= int(batch_threads) <= 64):
        raise ValueError('ELECTROCHEM_CPU_THREADS must be an integer from 1 to 64')
    launcher_type = AppLauncher
    if explicit_stage or batch_threads:
        # AppLauncher 2.2 filters these SimulationApp options out of kwargs.
        # Set them immediately before its normal app creation instead.
        class ExplicitStageLauncher(AppLauncher):
            def _create_app(self):
                if explicit_stage:
                    self._sim_app_config.update(create_new_stage=False, disable_viewport_updates=True)
                if batch_threads:
                    self._sim_app_config.update(limit_cpu_threads=int(batch_threads))
                super()._create_app()
        launcher_type = ExplicitStageLauncher
    app = launcher_type(**options).app
    if explicit_stage:
        import omni.usd
        context = omni.usd.get_context()
        if context.get_stage() is None:
            context.new_stage()
    return app
