import os
from pathlib import Path
from pydantic import BaseModel
from fastapi import APIRouter
from app.config import settings, BASE_DIR

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsPayload(BaseModel):
    volc_app_id: str = ""
    volc_access_token: str = ""
    volc_access_key: str = ""
    volc_secret_key: str = ""
    volc_cluster_id: str = ""
    volc_streaming_cluster_id: str = ""
    volc_file_cluster_id: str = ""
    dashscope_api_key: str = ""
    gemini_api_key: str = ""
    deepseek_api_key: str = ""
    default_asr_provider: str = "doubao"
    default_llm_provider: str = "deepseek"


def mask_key(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return k[:4] + "****" + k[-4:]


@router.get("")
def get_settings():
    # All credentials are masked in responses; plaintext keys never leave the server.
    return {
        "volc_app_id_masked": mask_key(settings.VOLC_APP_ID),
        "volc_access_token_masked": mask_key(settings.VOLC_ACCESS_TOKEN),
        "volc_access_key_masked": mask_key(settings.VOLC_ACCESS_KEY),
        "volc_secret_key_masked": mask_key(settings.VOLC_SECRET_KEY),
        "volc_cluster_id": settings.VOLC_CLUSTER_ID,
        "volc_streaming_cluster_id": settings.VOLC_STREAMING_CLUSTER_ID,
        "volc_file_cluster_id": settings.VOLC_FILE_CLUSTER_ID,
        "dashscope_api_key_masked": mask_key(settings.DASHSCOPE_API_KEY),
        "gemini_api_key_masked": mask_key(settings.GEMINI_API_KEY),
        "deepseek_api_key_masked": mask_key(settings.DEEPSEEK_API_KEY),
        "has_volc_configured": bool((settings.VOLC_APP_ID or settings.VOLC_ACCESS_KEY) and (settings.VOLC_ACCESS_TOKEN or settings.VOLC_SECRET_KEY)),
        "has_dashscope_configured": bool(settings.DASHSCOPE_API_KEY),
        "has_gemini_configured": bool(settings.GEMINI_API_KEY),
        "has_deepseek_configured": bool(settings.DEEPSEEK_API_KEY),
        "default_asr_provider": settings.DEFAULT_ASR_PROVIDER,
        "default_llm_provider": settings.DEFAULT_LLM_PROVIDER,
    }


@router.post("")
def update_settings(payload: SettingsPayload):
    env_path = BASE_DIR / ".env"
    existing_lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()

    env_dict = {}
    for line in existing_lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env_dict[k.strip()] = v.strip()

    # Update values if provided (don't overwrite with masked string or empty unless desired)
    if payload.volc_app_id and not payload.volc_app_id.startswith("****") and "****" not in payload.volc_app_id:
        env_dict["VOLC_APP_ID"] = payload.volc_app_id
        settings.VOLC_APP_ID = payload.volc_app_id

    if payload.volc_access_token and "****" not in payload.volc_access_token:
        env_dict["VOLC_ACCESS_TOKEN"] = payload.volc_access_token
        settings.VOLC_ACCESS_TOKEN = payload.volc_access_token

    if payload.volc_access_key and "****" not in payload.volc_access_key:
        env_dict["VOLC_ACCESS_KEY"] = payload.volc_access_key
        settings.VOLC_ACCESS_KEY = payload.volc_access_key

    if payload.volc_secret_key and "****" not in payload.volc_secret_key:
        env_dict["VOLC_SECRET_KEY"] = payload.volc_secret_key
        settings.VOLC_SECRET_KEY = payload.volc_secret_key

    if payload.volc_cluster_id:
        env_dict["VOLC_CLUSTER_ID"] = payload.volc_cluster_id.strip()
        settings.VOLC_CLUSTER_ID = payload.volc_cluster_id.strip()

    if payload.volc_streaming_cluster_id:
        env_dict["VOLC_STREAMING_CLUSTER_ID"] = payload.volc_streaming_cluster_id.strip()
        settings.VOLC_STREAMING_CLUSTER_ID = payload.volc_streaming_cluster_id.strip()

    if payload.volc_file_cluster_id:
        env_dict["VOLC_FILE_CLUSTER_ID"] = payload.volc_file_cluster_id.strip()
        settings.VOLC_FILE_CLUSTER_ID = payload.volc_file_cluster_id.strip()

    if payload.dashscope_api_key and not payload.dashscope_api_key.startswith("****"):
        env_dict["DASHSCOPE_API_KEY"] = payload.dashscope_api_key
        settings.DASHSCOPE_API_KEY = payload.dashscope_api_key

    if payload.gemini_api_key and not payload.gemini_api_key.startswith("****"):
        env_dict["GEMINI_API_KEY"] = payload.gemini_api_key
        settings.GEMINI_API_KEY = payload.gemini_api_key

    if payload.deepseek_api_key and not payload.deepseek_api_key.startswith("****"):
        env_dict["DEEPSEEK_API_KEY"] = payload.deepseek_api_key
        settings.DEEPSEEK_API_KEY = payload.deepseek_api_key

    if payload.default_asr_provider:
        env_dict["DEFAULT_ASR_PROVIDER"] = payload.default_asr_provider
        settings.DEFAULT_ASR_PROVIDER = payload.default_asr_provider

    if payload.default_llm_provider:
        env_dict["DEFAULT_LLM_PROVIDER"] = payload.default_llm_provider
        settings.DEFAULT_LLM_PROVIDER = payload.default_llm_provider

    # Write back to .env
    with open(env_path, "w", encoding="utf-8") as f:
        for k, v in env_dict.items():
            f.write(f"{k}={v}\n")

    return {"message": "系统设置与 API 密钥已成功保存并立即生效！"}
