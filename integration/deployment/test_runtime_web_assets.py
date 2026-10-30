"""Small bundle contract regression; no cloud, assets download or simulator."""
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_runtime_bundle import build


class RuntimeWebAssetsTests(unittest.TestCase):
    def test_workspace_assets_are_packaged_and_hash_manifested(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            names = ['copper_cell_node.py', '电化学浓差电池_轻量可视化原型.html',
                     'integration/web/lab_workspace.css', 'integration/web/lab_workspace.js',
                     'integration/web/hmf_live.js']
            for name in names:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic bundle fixture', encoding='utf-8')
            result = build(source, root / 'output', runtime_profile='isaacsim6_isaaclab3')
            with tarfile.open(result['archive']) as archive:
                manifest = json.load(archive.extractfile('deployment_manifest.json'))
                for name in names:
                    self.assertIn(name, archive.getnames())
                    self.assertIn(name, {row['path'] for row in manifest['files']})


if __name__ == '__main__':
    unittest.main()
