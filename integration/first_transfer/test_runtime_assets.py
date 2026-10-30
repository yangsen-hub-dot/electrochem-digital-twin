import tempfile
import unittest
from pathlib import Path

from runtime_assets import kit_material_path


class KitMaterialPathTest(unittest.TestCase):
    def test_both_installed_layouts(self):
        for layout in ('isaacsim/kit/mdl/core/Base', 'omni/mdl/core/Base'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                material = root / layout / 'OmniGlass.mdl'
                material.parent.mkdir(parents=True)
                material.touch()
                self.assertEqual(kit_material_path('OmniGlass.mdl', root), material)

    def test_missing_asset_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                kit_material_path('OmniSurface.mdl', Path(directory))

    def test_rejects_untrusted_material_names(self):
        with self.assertRaises(ValueError):
            kit_material_path('../../anything')


if __name__ == '__main__':
    unittest.main()
