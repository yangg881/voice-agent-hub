import os
from pathlib import Path
from fastapi import APIRouter, Body
from app.config import settings, BASE_DIR

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 这些字段在 GET 时返回脱敏值，POST 时跳过含 **** 的未修改值
MASKED_KEYS = {
    "VOLC_APP_ID", "VOLC_ACCESS_TOKEN", "VOLC_ACCESS_KEY",
    "VOLC_SECRET_KEY", "DASHSCOPE_API_KEY", "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY", "ACCESS_TOKEN",
}


def mask_key(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return k[:4] + "****" + k[-4:]


@router.get("")
def get_settings():
    """返回所有可配置项，密钥字段脱敏。key 与 .env 环境变量名一致（大写）。"""
    mapping = {
        "VOLC_APP_ID": settings.VOLC_APP_ID,
        "VOLC_ACCESS_TOKEN": settings.VOLC_ACCESS_TOKEN,
        "VOLC_ACCESS_KEY": settings.VOLC_ACCESS_KEY,
        "VOLC_SECRET_KEY": settings.VOLC_SECRET_KEY,
        "VOLC_CLUSTER_ID": settings.VOLC_CLUSTER_ID,
        "VOLC_STREAMING_CLUSTER_ID": settings.VOLC_STREAMING_CLUSTER_ID,
        "VOLC_FILE_CLUSTER_ID": settings.VOLC_FILE_CLUSTER_ID,
        "DASHSCOPE_API_KEY": settings.DASHSCOPE_API_KEY,
        "GEMINI_API_KEY": settings.GEMINI_API_KEY,
        "DEEPSEEK_API_KEY": settings.DEEPSEEK_API_KEY,
        "DEFAULT_ASR_PROVIDER": settings.DEFAULT_ASR_PROVIDER,
        "DEFAULT_LLM_PROVIDER": settings.DEFAULT_LLM_PROVIDER,
        "MAX_CONTENT_LENGTH": settings.MAX_CONTENT_LENGTH,
        "ACCESS_TOKEN": settings.ACCESS_TOKEN,
        "CORS_ORIGINS": settings.CORS_ORIGINS,
    }
    result = {}
    for k, v in mapping.items():
        result[k] = mask_key(v) if k in MASKED_KEYS else v
    result["has_volc_configured"] = bool(
        (settings.VOLC_APP_ID or settings.VOLC_ACCESS_KEY)
        and (settings.VOLC_ACCESS_TOKEN or settings.VOLC_SECRET_KEY)
    )
    result["has_dashscope_configured"] = bool(settings.DASHSCOPE_API_KEY)
    result["has_gemini_configured"] = bool(settings.GEMINI_API_KEY)
    result["has_deepseek_configured"] = bool(settings.DEEPSEEK_API_KEY)
    return result


@router.post("")
def update_settings(payload: dict = Body(...)):
    """
    接收任意 key=value（key 不区分大小写，统一转大写）。
    - 含 **** 的脱敏值自动跳过（用户未修改）
    - 空字符串的密钥字段自动跳过
    - 写回 .env 时保留原有注释、空行和顺序，仅替换匹配行；新 key 追加到末尾
    """
    env_path = BASE_DIR / ".env"
    existing_lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()

    updates = {}
    for k, v in payload.items():
        if v is None:
            continue
        k_upper = k.strip().upper()
        if not k_upper:
            continue
        v_str = str(v).strip()
        if "****" in v_str:
            continue  # 脱敏值，用户未修改
        if not v_str and k_upper in MASKED_KEYS:
            continue  # 空密钥不覆盖
        updates[k_upper] = v_str

    if not updates:
        return {"message": "没有需要更新的设置", "updated": []}

    # 逐行替换，保留注释和顺序
    new_lines = []
    found_keys = set()
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip().upper()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                found_keys.add(key)
                continue
        new_lines.append(line)

    # 追加不存在的新 key
    for k, v in updates.items():
        if k not in found_keys:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"{k}={v}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    return {
        "message": "设置已保存到 .env，重启服务后生效",
        "updated": sorted(updates.keys()),
    }
