"""Build a versioned cloud runtime from the authoritative local source (standard library only)."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path


RUNTIME_PROFILES = {
    "isaacsim5_isaaclab2": "integration/reference/MatterixIsaacLab2",
    "isaacsim6_isaaclab3": "integration/reference/Matterix",
}


def build(source: Path, output: Path, runtime_profile="isaacsim5_isaaclab2") -> dict:
    source = source.resolve()
    matterix_relative_path = RUNTIME_PROFILES[runtime_profile]
    selected: set[Path] = set()
    for relative in ("electrochemistry_simulation/electrochem_sim",
                     "electrochemistry_simulation/config", "integration/first_transfer",
                     "integration/coordinator", "integration/media_service", "integration/deployment",
                     "integration/process_validation", "integration/device_adapters"):
        for path in (source / relative).rglob("*"):
            if not path.is_file() or any(part in {"__pycache__", ".git", "validation"} for part in path.parts):
                continue
            if path.name.startswith("test_") or path.suffix not in {".py", ".json", ".sh", ".txt"}:
                continue
            selected.add(path)
    # Isaac Lab 2.x paired runtime pinned to the upstream pre-3.0 Matterix
    # commit. Keep the newer upstream checkout locally as an untouched reference.
    matterix = source / matterix_relative_path
    for path in matterix.rglob("*"):
        if not path.is_file() or any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        relative = path.relative_to(matterix)
        if str(relative).replace("\\", "/").startswith("source/matterix_assets/data/"):
            if "labware/beaker500ml/" not in str(relative).replace("\\", "/") and path.name not in {"LICENSE", "LICENSE.txt", "README.md"}:
                continue
        if path.suffix in {".py", ".toml", ".txt", ".md", ".usda", ".usd", ".usdz", ".png", ".jpg", ".json"} or path.name.startswith("LICENSE"):
            selected.add(path)
    for path in (source / "integration/reference/ChemLab-Assets").rglob("*"):
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts:
            selected.add(path)
    isaaclab = source / "integration/reference/IsaacLab"
    for name in (("isaaclab_assets", "isaaclab_tasks") if runtime_profile == "isaacsim5_isaaclab2" else ()):
        if not (isaaclab / "source" / name / "setup.py").is_file():
            raise ValueError(f"Required Isaac Lab 2.2.0 source package is missing: {name}")
        for path in (isaaclab / "source" / name).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix in {".py", ".toml", ".yaml", ".yml", ".json", ".md", ".txt"}:
                selected.add(path)
    for path in isaaclab.glob("LICENSE*"):
        if path.is_file(): selected.add(path)
    selected.add(source / "copper_cell_node.py")
    selected.add(source / "电化学浓差电池_轻量可视化原型.html")
    # The browser workspace keeps presentation assets separate from experiment logic.
    selected.add(source / "integration/web/lab_workspace.css")
    selected.add(source / "integration/web/lab_workspace.js")
    selected.add(source / "integration/web/hmf_live.js")
    files = []
    for path in sorted(selected):
        data = path.read_bytes()
        if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise ValueError(f"Required asset is an unresolved Git LFS pointer: {path}")
        files.append({"path": path.relative_to(source).as_posix(), "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()})
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + fingerprint
    manifest = {"release_id": release_id, "source_root": str(source),
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_policy": "local_authoritative_cloud_runtime_copy",
                "runtime_profile": runtime_profile,
                "matterix_relative_path": matterix_relative_path, "files": files}
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / (release_id + ".tar.gz")
    with tarfile.open(archive_path, "w:gz") as archive:
        for row in files:
            archive.add(source / row["path"], arcname=row["path"], recursive=False)
        data = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
        info = tarfile.TarInfo("deployment_manifest.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    result = {"release_id": release_id, "archive": str(archive_path),
              "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
              "file_count": len(files), "bytes": archive_path.stat().st_size}
    (output / "latest_bundle.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-profile", choices=tuple(RUNTIME_PROFILES), default="isaacsim5_isaaclab2")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.runtime_profile)))
