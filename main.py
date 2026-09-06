import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import traceback

try:
    from app.main import app
except BaseException as e:
    err = traceback.format_exc()
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    app = FastAPI(title="VoiceAgentHub Startup Diagnostic")
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def err_route(path: str = ""):
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;padding:2rem;background:#0f172a;color:#f8fafc;'>"
            f"<h2 style='color:#ef4444;'>VoiceAgentHub Startup Error</h2>"
            f"<pre style='background:#1e293b;padding:1rem;border-radius:8px;overflow:auto;'>{err}</pre>"
            f"</body></html>",
            status_code=200
        )
