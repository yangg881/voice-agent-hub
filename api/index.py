import sys
import traceback
from pathlib import Path

# Ensure root directory is in sys.path
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

try:
    from app.main import app
except Exception:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="VoiceAgentHub Serverless Fallback")
    err_trace = traceback.format_exc()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def error_route(path: str = ""):
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;padding:2rem;background:#0f172a;color:#f8fafc;'>"
            f"<h2 style='color:#ef4444;'>VoiceAgentHub Startup Error</h2>"
            f"<pre style='background:#1e293b;padding:1rem;border-radius:8px;overflow:auto;'>{err_trace}</pre>"
            f"</body></html>",
            status_code=500,
        )

