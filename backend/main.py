"""PYROTECH VISION backend entry point.

    python backend/main.py            start the API (from the repository root)
    python backend/main.py --check    self-check: packages, paths, model, detection on a sample image

Settings come from environment variables or a .env file (see ../.env.example).
Everything the backend needs is inside this repository (code, model, tracker config, data).
"""
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)          # makes "backend" importable as a package
# Ultralytics resolves some relative paths against the working directory; keep it stable.
os.chdir(BACKEND_DIR)

if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        from backend.selfcheck import run
        sys.exit(run())
    from backend.api.server import serve
    serve()
