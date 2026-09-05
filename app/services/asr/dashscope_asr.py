import asyncio
import json
import httpx
from typing import List
from app.config import settings
from app.services.asr.base import BaseAsrProvider, AsrSegmentData


class DashscopeAsrProvider(BaseAsrProvider):
    """
    阿里云 DashScope (通义听悟 / Paraformer-v2 / SenseVoice)
    支持角色分离 (diarization_enabled) 与时间戳。
    """
    TRANSCRIPTION_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
    TASK_QUERY_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or settings.DASHSCOPE_API_KEY

    async def transcribe(self, audio_file_path: str, recording_id: str) -> List[AsrSegmentData]:
        if not self.api_key:
            raise ValueError("未配置阿里云 DashScope API_KEY，请在设置中配置！")

        # DashScope can upload files or take URL. We can use file upload API or dashscope audio sdk
        # For direct HTTP REST:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }

        # First upload file via DashScope Files API if local
        file_url = await self._upload_dashscope_file(audio_file_path)

        payload = {
            "model": "paraformer-v2",
            "input": {
                "file_urls": [file_url],
            },
            "parameters": {
                "diarization_enabled": True,  # 开启说话人分离
                "timestamp_alignment_enabled": True,
            }
        }

        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(self.TRANSCRIPTION_URL, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"DashScope ASR 提交失败: {resp.text}")

            task_id = resp.json().get("output", {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"DashScope 未返回 task_id: {resp.text}")

            # Poll for result
            for _ in range(90):
                await asyncio.sleep(2)
                q_resp = await client.get(self.TASK_QUERY_URL.format(task_id=task_id), headers=headers)
                q_json = q_resp.json()
                task_status = q_json.get("output", {}).get("task_status")

                if task_status == "SUCCEEDED":
                    results_url = q_json.get("output", {}).get("results", [{}])[0].get("subtask_url")
                    if results_url:
                        sub_resp = await client.get(results_url)
                        return self._parse_dashscope_result(sub_resp.json())
                    # Or direct transcripts
                    transcripts = q_json.get("output", {}).get("results", [{}])[0].get("transcripts", [])
                    return self._parse_dashscope_transcripts(transcripts)

                elif task_status == "FAILED":
                    raise RuntimeError(f"DashScope ASR 转录失败: {q_json}")

            raise TimeoutError("DashScope ASR 转录超时")

    async def _upload_dashscope_file(self, file_path: str) -> str:
        # Uploads file to DashScope files API
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = "https://dashscope.aliyuncs.com/api/v1/files"
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(file_path, "rb") as f:
                files = {"file": (file_path, f, "audio/mpeg")}
                data = {"description": "voice_agent_audio"}
                resp = await client.post(url, headers=headers, files=files, data=data)
                if resp.status_code == 200:
                    file_id = resp.json().get("data", {}).get("id")
                    if file_id:
                        return f"fileid://{file_id}"
        # If file upload API not used, raise informative error
        raise RuntimeError("无法上传本地音频到 DashScope，请确保 API Key 有效或使用预处理公网 URL")

    def _parse_dashscope_result(self, data: dict) -> List[AsrSegmentData]:
        transcripts = data.get("transcripts", [])
        return self._parse_dashscope_transcripts(transcripts)

    def _parse_dashscope_transcripts(self, transcripts: list) -> List[AsrSegmentData]:
        results = []
        seq = 1
        for item in transcripts:
            sentences = item.get("sentences", [])
            for s in sentences:
                spk = s.get("speaker_id") or "1"
                text = s.get("text", "").strip()
                if text:
                    results.append(
                        AsrSegmentData(
                            speaker_id=f"说话人 {spk}",
                            start_ms=int(s.get("begin_time", 0)),
                            end_ms=int(s.get("end_time", 0)),
                            raw_text=text,
                            seq_order=seq,
                        )
                    )
                    seq += 1
        return results
