import os
from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent

def _get_data_dir() -> Path:
    # First try default local data directory
    candidate = BASE_DIR / "data"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        test_file = candidate / ".write_test"
        test_file.touch()
        test_file.unlink()
        return candidate
    except (OSError, PermissionError):
        # Read-only filesystem detected (e.g. Vercel, AWS Lambda)
        fallback = Path("/tmp/voice_agent_data")
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
        except Exception:
            return Path("/tmp")

DATA_DIR = _get_data_dir()
AUDIO_DIR = DATA_DIR / "audio"
RAW_AUDIO_DIR = AUDIO_DIR / "raw"
PROCESSED_AUDIO_DIR = AUDIO_DIR / "processed"
DB_DIR = DATA_DIR / "db"

for d in [AUDIO_DIR, RAW_AUDIO_DIR, PROCESSED_AUDIO_DIR, DB_DIR]:
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


class Settings(BaseSettings):
    APP_NAME: str = "VoiceAgentHub"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    # SQLite Database
    DATABASE_URL: str = f"sqlite:///{DB_DIR / 'voice_agent.db'}"

    VOLC_APP_ID: str = ""           # App ID
    VOLC_ACCESS_TOKEN: str = ""     # Token
    VOLC_ACCESS_KEY: str = ""       # AK (Access Key ID)
    VOLC_SECRET_KEY: str = ""       # SK (Secret Access Key)
    VOLC_CLUSTER_ID: str = "volc.bigasr.auc"  # 默认 Cluster / Resource ID (录音文件识别)
    VOLC_STREAMING_CLUSTER_ID: str = "volc.bigasr.sauc.duration"  # 专用流式对讲 WebSocket Cluster / Resource ID
    VOLC_FILE_CLUSTER_ID: str = "volc.bigasr.auc"       # 专用录音文件识别 Cluster / Resource ID


    # Alibaba DashScope (Qwen / Paraformer)
    DASHSCOPE_API_KEY: str = ""

    # Google Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com"

    # DeepSeek API (深度求索)
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # Model & Provider Preferences
    DEFAULT_ASR_PROVIDER: str = "doubao"  # "doubao", "dashscope", "gemini", "mock"
    DEFAULT_LLM_PROVIDER: str = "deepseek"  # "deepseek", "gemini", "dashscope", "doubao", "mock"
    CLEANING_MODEL: str = "deepseek-chat"
    AGENT_MODEL: str = "deepseek-chat"

    # Max upload file size: 500 MB
    MAX_CONTENT_LENGTH: int = 500 * 1024 * 1024

    # Optional access token. When empty, API is open (default). When set,
    # all /api routes require header X-Access-Token or ?token= query param.
    ACCESS_TOKEN: str = ""

    # CORS allowed origins, comma separated. "*" means any origin.
    CORS_ORIGINS: str = "*"

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
