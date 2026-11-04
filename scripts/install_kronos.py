from __future__ import annotations
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "third_party" / "Kronos"
URL = "https://github.com/shiyu-coder/Kronos.git"
REVISION = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"

def run(*args):
    subprocess.run(args, check=True)

if TARGET.exists():
    raise SystemExit(f"Refusing to overwrite existing third-party directory: {TARGET}; remove it intentionally first.")
TARGET.parent.mkdir(parents=True, exist_ok=True)
run("git", "clone", "--filter=blob:none", URL, str(TARGET))
run("git", "-C", str(TARGET), "checkout", "--detach", REVISION)
actual = subprocess.check_output(["git", "-C", str(TARGET), "rev-parse", "HEAD"], text=True).strip()
if actual != REVISION:
    raise SystemExit(f"Kronos revision mismatch: expected {REVISION}, got {actual}")
print(f"Installed Kronos source at pinned revision {actual}")
