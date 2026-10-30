"""Start a local frontend or an existing numerical backend. No cloud jobs are submitted."""
from pathlib import Path
import argparse
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("frontend", "hmf", "concentration"), default="frontend", nargs="?")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.mode in {"frontend", "hmf"}:
        subprocess.run([sys.executable, str(ROOT / "scripts/build_site.py")], check=True)
    if args.mode == "frontend":
        argv = [sys.executable, "-m", "http.server", str(args.port or 8080), "--bind", "127.0.0.1", "--directory", str(ROOT / "dist")]
    elif args.mode == "hmf":
        argv = [sys.executable, str(ROOT / "integration/web/hmf_local_server.py"), "--port", str(args.port or 8766), "--site-root", str(ROOT / "dist"), "--history-dir", str(ROOT / "var/hmf-history")]
    else:
        argv = [sys.executable, str(ROOT / "copper_cell_node.py"), "--host", "127.0.0.1", "--port", str(args.port or 50005), "--output-dir", str(ROOT / "var/concentration"), "--allowed-origin", "http://127.0.0.1:8080", "--allowed-origin", "null"]
    return subprocess.call(argv, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
