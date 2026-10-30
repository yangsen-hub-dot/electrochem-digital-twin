"""Exercise the real vendor property without starting Isaac Sim."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


def compatibility_class():
    source = (Path(__file__).resolve().parents[1] / 'reference/Matterix/source/'
              'matterix/matterix/envs/matterix_base_env.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))
    native = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MatterixBaseEnv')
    methods = [n for n in native.body if isinstance(n, ast.FunctionDef) and n.name == 'has_rtx_sensors']
    wrapper = ast.ClassDef(name='CompatibilityEnv', bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    namespace = {}
    exec(compile(module, str(source), 'exec'), namespace)
    return namespace['CompatibilityEnv']


class NativeRuntimeCompatibilityTests(unittest.TestCase):
    def test_lab3_initialization_can_assign_sensor_flag_including_false(self):
        env = compatibility_class()()
        env.sim = SimpleNamespace()
        env.has_rtx_sensors = True
        self.assertTrue(env.has_rtx_sensors)
        env.has_rtx_sensors = False
        self.assertFalse(env.has_rtx_sensors)

    def test_lab2_can_query_legacy_simulation_context_without_assignment(self):
        env = compatibility_class()()
        env.sim = SimpleNamespace(has_rtx_sensors=lambda: True)
        self.assertTrue(env.has_rtx_sensors)
        env.sim.has_rtx_sensors = False
        self.assertFalse(env.has_rtx_sensors)


if __name__ == '__main__':
    unittest.main()
