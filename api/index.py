import os
import sys
import traceback
from pathlib import Path

# Add project root to sys.path
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

try:
    from app.main import app as main_app
    app = main_app
except BaseException as e:
    err_tb = traceback.format_exc()
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="VoiceAgentHub Startup Diagnostic")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def error_handler(path: str = ""):
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;padding:2rem;background:#0f172a;color:#f8fafc;'>"
            f"<h2 style='color:#ef4444;'>VoiceAgentHub Vercel Startup Error</h2>"
            f"<p>An error occurred while importing <code>app.main</code>:</p>"
            f"<pre style='background:#1e293b;padding:1rem;border-radius:8px;overflow:auto;white-space:pre-wrap;color:#f87171;'>{err_tb}</pre>"
            f"</body></html>",
            status_code=200,
        )
