import asyncio
import base64
import gzip
import json
import logging
import os
import struct
import uuid
from typing import List, Optional, Union
import httpx
import websockets

from app.config import settings
from app.services.asr.base import BaseAsrProvider, AsrSegmentData

logger = logging.getLogger(__name__)

# Protocol constants for Volcengine V1 binary streaming protocol
PROTOCOL_VERSION = 0x01
HEADER_SIZE = 0x01

MESSAGE_TYPE_FULL_CLIENT_REQUEST = 0x01
MESSAGE_TYPE_AUDIO_ONLY_REQUEST = 0x02
MESSAGE_TYPE_FULL_SERVER_RESPONSE = 0x09
MESSAGE_TYPE_ERROR = 0x0F

SERIALIZATION_NONE = 0x00
SERIALIZATION_JSON = 0x01

COMPRESSION_NONE = 0x00
COMPRESSION_GZIP = 0x01

FLAG_NONE = 0x00
FLAG_HAS_SEQUENCE = 0x01
FLAG_LAST_PACKET = 0x02
FLAG_LAST_PACKET_HAS_SEQUENCE = 0x03


def _build_header(*, message_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def _encode_frame(*, header: bytes, payload: bytes) -> bytes:
    return header + len(payload).to_bytes(4, byteorder="big", signed=False) + payload


def _extract_result_text(payload_obj: dict) -> str:
    """Extract recognized text from various response shapes"""
    result = payload_obj.get("result")
    if isinstance(result, dict):
        text = result.get("text", "")
        if text:
            return str(text).strip()
        # utterances
        utts = result.get("utterances", [])
        if utts:
            return "".join(u.get("text", "") for u in utts).strip()
    elif isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return str(first.get("text", "")).strip()
        return " ".join(str(r) for r in result).strip()

    # direct utterances at root
    utts = payload_obj.get("utterances") or payload_obj.get("resp", {}).get("utterances")
    if utts:
        return "".join(u.get("text", "") for u in utts).strip()

    # fallback
    return str(payload_obj.get("text", "")).strip()


def _parse_server_message(message: Union[bytes, str]) -> str:
    if isinstance(message, str):
        try:
            payload_obj = json.loads(message)
            return _extract_result_text(payload_obj)
        except Exception:
            return ""

    if len(message) < 4:
        return ""

    header = message[:4]
    message_type = (header[1] >> 4) & 0x0F
    flags = header[1] & 0x0F
    compression = header[2] & 0x0F
    serialization = (header[2] >> 4) & 0x0F

    if message_type == MESSAGE_TYPE_FULL_SERVER_RESPONSE:
        has_sequence = flags in {FLAG_HAS_SEQUENCE, FLAG_LAST_PACKET_HAS_SEQUENCE}
        size_offset = 8 if has_sequence else 4
        if len(message) < size_offset + 4:
            return ""
        payload_size = int.from_bytes(message[size_offset : size_offset + 4], byteorder="big", signed=False)
        payload_start = size_offset + 4
        payload = message[payload_start : payload_start + payload_size]
        if payload_size == 0 or not payload:
            return ""
        if compression == COMPRESSION_GZIP:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        if serialization == SERIALIZATION_JSON:
            try:
                payload_obj = json.loads(payload.decode("utf-8"))
                return _extract_result_text(payload_obj)
            except Exception:
                return ""
        return ""

    if message_type == MESSAGE_TYPE_ERROR:
        if len(message) < 12:
            raise RuntimeError(f"火山引擎 ASR 异常响应 (消息过短): {message.hex()}")
        error_code = int.from_bytes(message[4:8], byteorder="big", signed=False)
        error_size = int.from_bytes(message[8:12], byteorder="big", signed=False)
        error_payload = message[12 : 12 + error_size]
        if compression == COMPRESSION_GZIP:
            try:
                error_payload = gzip.decompress(error_payload)
            except Exception:
                pass
        error_text = error_payload.decode("utf-8", errors="replace")
        if "requested resource not granted" in error_text:
            raise RuntimeError(
                f"火山引擎 ASR 报错 (错误码 {error_code}): 资源未授权 (requested resource not granted)。"
                f"请在火山引擎控制台为 AppID 开通对应的语音识别服务，或核对 Resource ID。"
            )
        raise RuntimeError(f"火山引擎 ASR 服务端报错 (错误码 {error_code}): {error_text}")

    return ""


class VolcStreamAsrProvider:
    """
    火山引擎大模型流式语音识别 (Streaming ASR via WebSocket)
    专用于实时对讲边说边识别，低延迟返回文本片段。
    """
    ENDPOINTS = [
        "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
        "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
        "wss://openspeech.bytedance.com/api/v2/asr",
    ]

    def __init__(
        self,
        app_id: Optional[str] = None,
        token: Optional[str] = None,
        cluster_id: Optional[str] = None,
    ):
        self.app_id = (app_id or settings.VOLC_APP_ID).strip()
        self.token = (token or settings.VOLC_ACCESS_TOKEN or settings.VOLC_SECRET_KEY).strip()
        self.cluster_id = (
            cluster_id
            or getattr(settings, "VOLC_STREAMING_CLUSTER_ID", "")
            or settings.VOLC_CLUSTER_ID
        ).strip()

    def _build_full_client_request(self) -> bytes:
        payload = {
            "user": {"uid": "voice_agent_user"},
            "audio": {
                "format": "wav",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
                "language": "zh-CN",
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "result_type": "single",
            },
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        compressed = gzip.compress(payload_bytes)
        header = _build_header(
            message_type=MESSAGE_TYPE_FULL_CLIENT_REQUEST,
            flags=FLAG_NONE,
            serialization=SERIALIZATION_JSON,
            compression=COMPRESSION_GZIP,
        )
        return _encode_frame(header=header, payload=compressed)

    def _build_audio_frame(self, chunk: bytes, is_last: bool) -> bytes:
        compressed = gzip.compress(chunk)
        header = _build_header(
            message_type=MESSAGE_TYPE_AUDIO_ONLY_REQUEST,
            flags=FLAG_LAST_PACKET if is_last else FLAG_NONE,
            serialization=SERIALIZATION_NONE,
            compression=COMPRESSION_GZIP,
        )
        return _encode_frame(header=header, payload=compressed)

    async def transcribe_chunk(self, wav_file_path: str) -> str:
        """
        Transcribe a single audio chunk via streaming WebSocket.
        """
        if not self.app_id or not self.token:
            raise ValueError("未配置火山引擎 AppID 或 Token！请在系统设置中配置。")

        if not os.path.exists(wav_file_path):
            raise FileNotFoundError(f"音频文件不存在: {wav_file_path}")

        with open(wav_file_path, "rb") as f:
            raw = f.read()

        # Extract PCM from WAV header if present
        pcm = raw[44:] if raw[:4] == b"RIFF" else raw
        if not pcm:
            return ""

        headers = {
            "X-Api-Resource-Id": self.cluster_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
            "X-Api-Connect-Id": str(uuid.uuid4()),
            "X-Api-App-Key": self.app_id,
            "X-Api-Access-Key": self.token,
        }

        last_error = None
        for endpoint in self.ENDPOINTS:
            try:
                text = await self._stream_to_endpoint(endpoint, headers, pcm)
                if text:
                    return text
            except Exception as e:
                logger.warning("Stream ASR endpoint %s failed: %s", endpoint, e)
                last_error = e

        if last_error:
            raise last_error
        return ""

    async def _stream_to_endpoint(self, endpoint: str, headers: dict, pcm: bytes) -> str:
        async with websockets.connect(endpoint, additional_headers=headers, open_timeout=6) as ws:
            # 1. Send full client request
            init_req = self._build_full_client_request()
            await ws.send(init_req)

            final_text = ""
            # 2. Slice PCM into 3200-byte packets (100ms each)
            chunk_size = 3200
            for i in range(0, len(pcm), chunk_size):
                slice_bytes = pcm[i : i + chunk_size]
                is_last = (i + chunk_size >= len(pcm))
                frame = self._build_audio_frame(slice_bytes, is_last=is_last)
                await ws.send(frame)

                # Non-blocking receive
                try:
                    msg = await asyncio.wait_for(ws.recv(decode=False), timeout=0.01)
                    t = _parse_server_message(msg)
                    if t:
                        final_text = t
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(0.01)

            # 3. Wait for final recognition result
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(ws.recv(decode=False), timeout=1.5)
                    t = _parse_server_message(msg)
                    if t:
                        final_text = t
                except asyncio.TimeoutError:
                    break

            return final_text


class VolcBigModelAucProvider:
    """
    火山引擎大模型录音文件识别 (BigModel AUC API v3)
    用于音频切片与完整录音文件的高精、低延迟异步转写 (实机验证通过)。
    """
    SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
    QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"

    def __init__(
        self,
        app_id: Optional[str] = None,
        token: Optional[str] = None,
        resource_id: Optional[str] = None,
    ):
        self.app_id = (app_id or settings.VOLC_APP_ID).strip()
        self.token = (token or settings.VOLC_ACCESS_TOKEN or settings.VOLC_SECRET_KEY).strip()
        self.resource_id = (
            resource_id
            or getattr(settings, "VOLC_FILE_CLUSTER_ID", "")
            or settings.VOLC_CLUSTER_ID
            or "volc.bigasr.auc"
        ).strip()
        # Default to volc.bigasr.auc if empty or if sauc streaming cluster was set
        if not self.resource_id or "sauc" in self.resource_id:
            self.resource_id = "volc.bigasr.auc"

    async def transcribe_file(self, audio_file_path: str) -> List[AsrSegmentData]:
        if not self.app_id or not self.token:
            raise ValueError("未配置火山引擎 AppID 或 Token！请在系统设置中配置。")

        if not os.path.exists(audio_file_path):
            raise FileNotFoundError(f"音频文件不存在: {audio_file_path}")

        with open(audio_file_path, "rb") as f:
            raw_bytes = f.read()

        b64_audio = base64.b64encode(raw_bytes).decode("utf-8")
        task_id = str(uuid.uuid4())
        headers = {
            "X-Api-App-Key": self.app_id,
            "X-Api-Access-Key": self.token,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1",
        }
        payload = {
            "user": {"uid": "voice_agent_user"},
            "audio": {
                "format": "wav",
                "data": b64_audio,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "show_utterances": True,
                "result_type": "full",
            },
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            submit_resp = await client.post(self.SUBMIT_URL, headers=headers, json=payload)
            if submit_resp.status_code != 200:
                raise RuntimeError(f"火山引擎 AUC Submit 失败 ({submit_resp.status_code}): {submit_resp.text}")

            status_code_hdr = submit_resp.headers.get("x-api-status-code")
            if status_code_hdr and status_code_hdr != "20000000":
                msg_hdr = submit_resp.headers.get("x-api-message", "")
                raise RuntimeError(f"火山引擎 AUC Submit 错误 ({status_code_hdr}): {msg_hdr}")

            # Poll for result
            query_headers = {
                "X-Api-App-Key": self.app_id,
                "X-Api-Access-Key": self.token,
                "X-Api-Resource-Id": self.resource_id,
                "X-Api-Request-Id": task_id,
            }

            for _ in range(25):  # up to 20 seconds
                await asyncio.sleep(0.8)
                query_resp = await client.post(self.QUERY_URL, headers=query_headers, json={})
                q_code = query_resp.headers.get("x-api-status-code")

                if q_code == "20000000":
                    data = query_resp.json()
                    res = data.get("result", {})
                    full_text = res.get("text", "").strip()
                    utterances = res.get("utterances", [])

                    if utterances:
                        segments = []
                        for i, u in enumerate(utterances):
                            u_text = u.get("text", "").strip()
                            if u_text:
                                segments.append(
                                    AsrSegmentData(
                                        speaker_id="发言人 1",
                                        start_ms=u.get("start_time", 0),
                                        end_ms=u.get("end_time", 0),
                                        raw_text=u_text,
                                        seq_order=i + 1,
                                    )
                                )
                        if segments:
                            return segments

                    if full_text:
                        return [
                            AsrSegmentData(
                                speaker_id="发言人 1",
                                start_ms=0,
                                end_ms=int(data.get("audio_info", {}).get("duration", 5000)),
                                raw_text=full_text,
                                seq_order=1,
                            )
                        ]
                q_msg = query_resp.headers.get("x-api-message", "")
                if (
                    q_code in ["20000001", "20000002", "45000010", "45000011"]
                    or "processing" in q_msg.lower()
                    or "requested grant not found" in q_msg
                ):
                    continue

                # 20000003: [Normal silence audio] Handle response: no valid speech in audio
                # This is normal silence/empty audio, not an error! Return empty segments.
                if q_code == "20000003" or "no valid speech" in q_msg.lower() or "silence audio" in q_msg.lower():
                    logger.info("Volcengine AUC returned normal silence audio (20000003): %s", q_msg)
                    return []

                if q_code and q_code != "20000000":
                    raise RuntimeError(f"火山引擎 AUC Query 报错 ({q_code}): {q_msg}")

        raise TimeoutError("火山引擎录音转写任务超时未返回结果")



class VolcAsrProvider(BaseAsrProvider):
    """
    火山引擎 ASR 综合 Provider (兼容 BaseAsrProvider 接口)
    - 优先使用大模型录音文件识别 (VolcBigModelAucProvider)，稳定高精。
    - 兼容流式 WebSocket 接入。
    """
    def __init__(
        self,
        app_id: Optional[str] = None,
        token: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        cluster_id: Optional[str] = None,
    ):
        self.app_id = (app_id or settings.VOLC_APP_ID).strip()
        self.token = (token or settings.VOLC_ACCESS_TOKEN or settings.VOLC_SECRET_KEY).strip()
        self.access_key = (access_key or settings.VOLC_ACCESS_KEY).strip()
        self.secret_key = (secret_key or settings.VOLC_SECRET_KEY).strip()
        self.cluster_id = (
            cluster_id
            or getattr(settings, "VOLC_FILE_CLUSTER_ID", "")
            or settings.VOLC_CLUSTER_ID
            or "volc.bigasr.auc"
        ).strip()
        self.auc_provider = VolcBigModelAucProvider(
            app_id=self.app_id,
            token=self.token,
            resource_id=self.cluster_id if "auc" in self.cluster_id else "volc.bigasr.auc",
        )
        self.stream_provider = VolcStreamAsrProvider(
            app_id=self.app_id,
            token=self.token,
            cluster_id=getattr(settings, "VOLC_STREAMING_CLUSTER_ID", "") or "volc.bigasr.sauc.duration",
        )

    async def transcribe(self, audio_file_path: str, recording_id: str) -> List[AsrSegmentData]:
        if not self.app_id or not self.token:
            raise ValueError("未配置火山引擎凭证！请在设置中配置 AppID / Token。")

        try:
            segments = await self.auc_provider.transcribe_file(audio_file_path)
            if segments:
                return segments
        except Exception as auc_err:
            logger.warning("BigModel AUC failed: %s, checking streaming fallback...", auc_err)
            try:
                text = await self.stream_provider.transcribe_chunk(audio_file_path)
                if text:
                    return [
                        AsrSegmentData(
                            speaker_id="发言人 1",
                            start_ms=0,
                            end_ms=5000,
                            raw_text=text,
                            seq_order=1,
                        )
                    ]
            except Exception:
                pass
            raise auc_err

        return []

