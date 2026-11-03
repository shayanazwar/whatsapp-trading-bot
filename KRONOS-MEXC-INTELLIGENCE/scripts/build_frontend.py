from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
source = root / "frontend" / "dist"
target = root / "app" / "static"
if not (source / "index.html").exists():
    raise SystemExit("frontend/dist/index.html not found; run npm --prefix frontend run build first")
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source, target)
print(f"Copied frontend build into {target}")
