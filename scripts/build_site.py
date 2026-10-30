"""Build the public frontend from the homepage and existing experiment workspace.

The allowlist deliberately excludes backend code, settings and runtime records.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path):
    output = output.resolve()
    if output == ROOT or output in ROOT.parents:
        raise ValueError("Output must be a separate build directory")
    output.mkdir(parents=True, exist_ok=True)
    for item in (ROOT / "frontend").rglob("*"):
        if item.is_file():
            target = output / item.relative_to(ROOT / "frontend")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    for name in ("lab_workspace.css", "lab_workspace.js", "hmf_live.js"):
        target = output / "integration/web" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "integration/web" / name, target)
    html = (ROOT / "电化学浓差电池_轻量可视化原型.html").read_text(encoding="utf-8")
    html = html.replace("<title>电化学浓差电池｜轻量可视化原型</title>", "<title>实验工作台｜实验数字孪生</title>")
    html = html.replace("</head>", '<link rel="stylesheet" href="assets/workbench.css">\n</head>')
    html = html.replace("<main>", '<main>\n<a class="public-home-link" href="index.html">← 实验数字孪生 / 平台首页</a>', 1)
    html = html.replace('<script src="integration/web/hmf_live.js">', '<script src="assets/public-connection.js"></script>\n<script src="integration/web/hmf_live.js">')
    (output / "workbench.html").write_text(html, encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Built public frontend: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    build(parser.parse_args().output)
