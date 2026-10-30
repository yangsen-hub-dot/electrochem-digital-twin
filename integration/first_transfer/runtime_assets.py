"""Resolve installed Kit material assets without changing their optical parameters."""
from pathlib import Path
import sysconfig


def kit_material_path(name: str, site_packages: Path | None = None) -> Path:
    """Support both Isaac Sim 5 pip and the older isaacsim/kit layout.

    Missing MDL remains a hard error: there is deliberately no opaque fallback.
    """
    if name not in {"OmniGlass.mdl", "OmniSurface.mdl"}:
        raise ValueError("Unsupported Kit material name")
    root = Path(sysconfig.get_paths()["purelib"]) if site_packages is None else Path(site_packages)
    candidates = [root / "isaacsim/kit/mdl/core/Base" / name,
                  root / "omni/mdl/core/Base" / name]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(f"Required RTX material {name} is not installed; refusing opaque fallback")
