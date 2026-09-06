import os
import sys
import traceback
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

# Add project root to sys.path
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

app = FastAPI(title="VoiceAgentHub")

# 1. Health check endpoint
@app.get("/api/health")
def health_check():
    auth_enabled = False
    app_version = "1.0.0"
    try:
        from app.config import settings
        auth_enabled = bool(settings.ACCESS_TOKEN)
        app_version = settings.APP_VERSION
    except Exception:
        pass
    return {
        "status": "healthy",
        "app": "VoiceAgentHub",
        "version": app_version,
        "auth_enabled": auth_enabled,
        "runtime": "vercel-serverless",
    }

# 2. Detailed Diagnostic endpoint
@app.get("/api/diagnostic")
def diagnostic():
    diag = {
        "python_version": sys.version,
        "root_path": str(root),
        "root_exists": root.exists(),
        "dir_contents": [p.name for p in root.iterdir()] if root.exists() else [],
    }
    try:
        from app.config import settings, DATA_DIR
        diag["config"] = {
            "DATA_DIR": str(DATA_DIR),
            "DATABASE_URL": settings.DATABASE_URL,
            "IS_VERCEL": bool(os.environ.get("VERCEL")),
        }
    except Exception:
        diag["config_error"] = traceback.format_exc()
    try:
        from app.database import init_db
        init_db()
        diag["database"] = "Initialized successfully"
    except Exception:
        diag["database_error"] = traceback.format_exc()
    return diag

# 3. Static files direct handler (safe on read-only serverless, zero external async I/O dependency)
@app.get("/static/{file_path:path}")
def serve_static(file_path: str):
    target = root / "app" / "static" / file_path
    if target.exists() and target.is_file():
        ext = target.suffix.lower()
        content_type = "application/javascript" if ext == ".js" else (
            "text/css" if ext == ".css" else (
                "text/html" if ext in [".html", ".htm"] else (
                    "image/svg+xml" if ext == ".svg" else (
                        "image/png" if ext == ".png" else "application/octet-stream"
                    )
                )
            )
        )
        return Response(content=target.read_bytes(), media_type=content_type)
    raise HTTPException(status_code=404, detail=f"Static file '{file_path}' not found")

# 4. Frontend Root
@app.get("/")
def index_page():
    index_file = root / "app" / "static" / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;padding:2rem;text-align:center;'>"
        "<h2>VoiceAgentHub API Service is Running</h2>"
        "<p>Frontend index.html was not found in the bundle.</p>"
        "<p><a href='/api/health'>Check /api/health</a> | <a href='/api/diagnostic'>Check /api/diagnostic</a></p>"
        "</body></html>"
    )

# 5. Include backend API routers with resilient error handling
try:
    from app.routes.recordings import router as recordings_router
    from app.routes.projects import router as projects_router
    from app.routes.settings import router as settings_router
    from app.routes.actions import router as actions_router

    app.include_router(recordings_router)
    app.include_router(projects_router)
    app.include_router(settings_router)
    app.include_router(actions_router)
except Exception as e:
    import logging
    logging.warning(f"Error loading backend routers: {e}")
