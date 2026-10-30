"""Generate a reviewable overlay ONLY. Never installs, opens, deploys or runs it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = '电化学浓差电池_轻量可视化原型.html'
RUNNER = 'integration/first_transfer/matterix_first_transfer.py'
SERVER = 'integration/coordinator/cloud_experiment_server.py'


def once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('Integration anchor changed; review manually: ' + before[:100])
    return text.replace(before, after, 1)


def prepare(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Overlay must be outside the live source tree')
    originals = {relative: (source / relative).read_bytes() for relative in (WEB, RUNNER, SERVER)}
    web, runner, server = [originals[key].decode('utf-8-sig') for key in (WEB, RUNNER, SERVER)]
    integrated = ('const livePreview = window.ElectrochemLivePreview.mount' in web,
                  'from preview_frames import optional_publisher' in runner,
                  'from preview_reader import preview_payload' in server)
    if any(integrated):
        if not all(integrated):
            raise ValueError('Partial integration; review manually before preparing a copy')
        files = dict(originals)
        for name in ('integration/first_transfer/preview_frames.py', 'integration/coordinator/preview_reader.py'):
            files[name] = (source / name).read_bytes()
        for path in (source/'integration/process_validation').glob('*'):
            if path.is_file() and path.suffix in {'.py','.json'} and not path.name.startswith('test_'):
                files[path.relative_to(source).as_posix()] = path.read_bytes()
        return write_overlay(source, output, originals, files)
    web = once(web, '  </style>', (HERE / 'preview_panel.css').read_text('utf-8') + '\n  </style>')
    web = once(web, '        <div class="video-frame">',
               (HERE / 'preview_panel.html').read_text('utf-8') + '\n        <div class="video-frame">')
    # A plain embedded script keeps file:// operation self-contained.
    at = web.index('  <script>')
    web = web[:at] + '  <script>\n' + (HERE / 'preview_client.js').read_text('utf-8') + '\n  </script>\n' + web[at:]
    web = once(web, '      async function renderCloudProcess(data) {',
               '      const livePreview = window.ElectrochemLivePreview.mount(() => ({endpoint:baseUrl(), token:$("video-access-code").value.trim()}));\n'
               '      async function renderCloudProcess(data) {')
    web = once(web, '            await renderCloudProcess(data);',
               '            livePreview.observe(data);\n            await renderCloudProcess(data);')
    web = once(web, '      disconnect.addEventListener("click", () => {',
               '      disconnect.addEventListener("click", () => {\n        livePreview.disconnect();')

    runner = once(runner, 'from transfer_session import TransferSession',
                  'from preview_frames import optional_publisher\nfrom transfer_session import TransferSession')
    runner = once(runner, 'parser = argparse.ArgumentParser()',
                  'parser = argparse.ArgumentParser()\nparser.add_argument("--live-preview", action="store_true", help="Publish bounded JPEG/confirmed-state packets alongside the full cloud MP4")')
    runner = once(runner, 'env = rgb = product = writer = None',
                  'preview = None\npreview_terminal = "failed"\nenv = rgb = product = writer = None')
    runner = once(runner, '    writer = imageio.get_writer(partial, fps=fps, codec="libx264",',
                  '    preview = optional_publisher(run_dir, session.run_id, enabled=args.live_preview and not args.preview_only)\n'
                  '    writer = imageio.get_writer(partial, fps=fps, codec="libx264",')
    runner = once(runner, '            writer.append_data(np.asarray(img))',
                  '            if preview is not None and preview.due():\n'
                  '                try:\n'
                  '                    preview.publish(img, session.snapshot(), frame_index=frames, simulation_time_s=steps * dt, phase=current_label, command=command)\n'
                  '                except Exception:\n'
                  '                    pass  # Preview is best-effort; it must not alter receipts or recording.\n'
                  '            writer.append_data(np.asarray(img))')
    runner = once(runner, '    result_kind = "SEGMENT_DIAGNOSTIC_COMPLETED" if diagnostic_run else "PROCESS_COMPLETED"',
                  '    preview_terminal = "ended"\n    result_kind = "SEGMENT_DIAGNOSTIC_COMPLETED" if diagnostic_run else "PROCESS_COMPLETED"')
    runner = once(runner, 'finally:\n    if writer is not None:',
                  'finally:\n    if preview is not None:\n        try:\n            preview.close(preview_terminal)\n        except Exception:\n            pass\n    if writer is not None:')

    server = once(server, "'--include-measurement']", "'--include-measurement', '--live-preview']")
    server = once(server, "                    self.send_json(200, batch_service.capabilities())",
                  "                    self.send_json(200, {**batch_service.capabilities(), 'live_preview': {'available': True, 'transport': 'latest_jpeg_with_same_frame_state', 'path': '/preview', 'target_interval_s': 0.5}})")
    server = once(server, '                elif url.path == "/steps":',
                  '                elif url.path == "/preview":\n'
                  '                    from preview_reader import preview_payload\n'
                  '                    after = int(parse_qs(url.query).get("after_sequence", ["0"])[0])\n'
                  '                    self.send_json(200, preview_payload(coordinator.runs_root, run_id, after))\n'
                  '                elif url.path == "/steps":')
    files = {WEB: web.encode('utf-8'), RUNNER: runner.encode('utf-8'), SERVER: server.encode('utf-8'),
             'integration/first_transfer/preview_frames.py': (HERE / 'preview_frames.py').read_bytes(),
             'integration/coordinator/preview_reader.py': (HERE / 'preview_reader.py').read_bytes()}
    return write_overlay(source, output, originals, files)


def write_overlay(source, output, originals, files):
    # Compile only: neither coordinator nor Isaac Sim is imported or executed here.
    for name, content in files.items():
        if name.endswith('.py'):
            compile(content.decode('utf-8'), name, 'exec')
    output.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest = {'status':'staged_only_not_installed', 'source':str(source),
                'original_sha256':{name:hashlib.sha256(data).hexdigest() for name,data in originals.items()},
                'overlay_sha256':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()},
                'preserved_originals':all((source / name).read_bytes() == data for name,data in originals.items()),
                'deployed':False, 'simulation_started':False, 'browser_opened':False}
    (output / 'staged_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.output), ensure_ascii=False, indent=2))
