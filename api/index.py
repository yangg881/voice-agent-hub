import os
import sys
import traceback
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

app = FastAPI(title="VoiceAgentHub")

@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "app": "VoiceAgentHub",
        "runtime": "vercel-serverless",
        "python": sys.version,
    }

@app.get("/api/test-imports")
def test_imports():
    results = {}
    for mod in [
        "app.config",
        "app.database",
        "app.services.audio_service",
        "app.services.pipeline",
        "app.routes.recordings",
        "app.routes.projects",
        "app.routes.settings",
        "app.routes.actions",
    ]:
        try:
            __import__(mod)
            results[mod] = "OK"
        except BaseException:
            results[mod] = traceback.format_exc()
    return results

@app.get("/static/{file_path:path}")
def serve_static(file_path: str):
    target = root / "app" / "static" / file_path
    if target.exists() and target.is_file():
        ext = target.suffix.lower()
        content_type = "application/javascript" if ext == ".js" else (
            "text/css" if ext == ".css" else (
                "text/html" if ext in [".html", ".htm"] else "application/octet-stream"
            )
        )
        return Response(content=target.read_bytes(), media_type=content_type)
    raise HTTPException(status_code=404, detail="Not found")

@app.get("/")
def home():
    index_file = root / "app" / "static" / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>VoiceAgentHub Serverless Ready</h1>")
