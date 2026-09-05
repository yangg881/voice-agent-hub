import base64
import json
import httpx
from typing import List
from app.config import settings
from app.services.asr.base import BaseAsrProvider, AsrSegmentData


class GeminiAsrProvider(BaseAsrProvider):
    """
    Google Gemini 原生多模态音频识别 (带说话人角色与时间戳)
    """
    def __init__(self, api_key: str = None):
        self.api_key = api_key or settings.GEMINI_API_KEY

    async def transcribe(self, audio_file_path: str, recording_id: str) -> List[AsrSegmentData]:
        if not self.api_key:
            raise ValueError("未配置 Google Gemini API_KEY，请在设置中配置！")

        with open(audio_file_path, "rb") as f:
            audio_bytes = f.read()

        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        url = f"{settings.GEMINI_BASE_URL}/v1beta/models/gemini-2.5-flash:generateContent?key={self.api_key}"

        prompt = (
            "你是一个极其精准的 ASR 语音识别与说话人角色分离引擎。\n"
            "请识别这段音频并输出带角色（Speaker 1, Speaker 2...）和时间戳（start_ms, end_ms）的逐字稿。\n"
            "【规则】：\n"
            "1. 务必保留所有原汁原味的口语、语气词（嗯、啊、那个、然后）和口吃重复。\n"
            "2. 必须以纯 JSON 数组格式返回，不要包含 markdown 标记或任何多余文字：\n"
            '[{"speaker_id": "说话人 1", "start_ms": 0, "end_ms": 4500, "raw_text": "..."}]'
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/mp3",
                                "data": audio_b64
                            }
                        },
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "response_mime_type": "application/json"
            }
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini ASR 识别失败: {resp.text}")

            result = resp.json()
            text_content = result.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "[]")

            try:
                data = json.loads(text_content.strip())
                segments = []
                for idx, item in enumerate(data):
                    segments.append(
                        AsrSegmentData(
                            speaker_id=item.get("speaker_id", f"说话人 {idx%2 + 1}"),
                            start_ms=int(item.get("start_ms", 0)),
                            end_ms=int(item.get("end_ms", 0)),
                            raw_text=item.get("raw_text", item.get("text", "")).strip(),
                            seq_order=idx + 1
                        )
                    )
                return segments
            except Exception as e:
                raise RuntimeError(f"解析 Gemini 返回数据失败: {e}\n原文: {text_content}")
