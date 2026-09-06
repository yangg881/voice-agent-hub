import os
import sys
from pathlib import Path

# Ensure project root is in sys.path regardless of execution context
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from app.config import settings, BASE_DIR
from app.database import init_db
from app.routes.recordings import router as recordings_router
from app.routes.settings import router as settings_router
from app.routes.projects import router as projects_router
from app.routes.actions import router as actions_router


async def verify_access_token(request: Request):
    """
    Optional API access guard. Disabled when ACCESS_TOKEN is empty (default).
    Accepts the token via X-Access-Token header or ?token= query param
    (query param is needed for <audio> elements that cannot send headers).
    """
    if not settings.ACCESS_TOKEN:
        return
    token = request.headers.get("X-Access-Token") or request.query_params.get("token")
    if token != settings.ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="未授权访问：缺少或错误的访问令牌")


# Initialize SQLite tables safely on startup (serverless & long-running friendly)
try:
    init_db()
except Exception as e:
    import logging
    logging.getLogger("uvicorn").warning(f"Database init notice: {e}")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
)

# CORS: same-origin frontend does not need CORS; keep it spec-valid.
# NOTE: allow_origins=["*"] must not be combined with allow_credentials=True.
_cors_origins = [o.strip() for o in (settings.CORS_ORIGINS or "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Routers (access guard applies to all API routes)
app.include_router(recordings_router, dependencies=[Depends(verify_access_token)])
app.include_router(projects_router, dependencies=[Depends(verify_access_token)])
app.include_router(settings_router, dependencies=[Depends(verify_access_token)])
app.include_router(actions_router, dependencies=[Depends(verify_access_token)])

# Mount Static directory
static_dir = BASE_DIR / "app" / "static"
try:
    static_dir.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/api/health")
def health_check():
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "auth_enabled": bool(settings.ACCESS_TOKEN),
    }


@app.get("/")
def index_page():
    index_file = static_dir / "index.html"
    if index_file.exists():
        try:
            return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
        except Exception:
            return FileResponse(str(index_file))
    return {"message": "VoiceAgentHub API Service is running."}
