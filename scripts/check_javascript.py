"""Check every bundled JavaScript file; no npm packages or frontend build needed."""

import shutil
import subprocess
from pathlib import Path


def main():
    node = shutil.which("node")
    if not node:
        raise SystemExit("Install Node.js to run JavaScript syntax checks.")
    files = sorted((Path(__file__).resolve().parents[1] / "src/quotamesh/ui/static").rglob("*.js"))
    for path in files:
        subprocess.run([node, "--check", str(path)], check=True)
    print(f"JavaScript syntax: {len(files)} files passed")


if __name__ == "__main__":
    main()
