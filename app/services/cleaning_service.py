import asyncio
import logging
import re
import json
import httpx
from typing import List
from pydantic import BaseModel
from app.config import settings
from app.services.asr.base import AsrSegmentData

logger = logging.getLogger(__name__)


class PolishedSegmentData(BaseModel):
    speaker_id: str
    start_ms: int
    end_ms: int
    polished_text: str
    seq_order: int


class CleaningService:
    """
    口语规整与清洗抛光引擎 (Text Polishing Engine)
    使用快速、低成本的 Flash 模型进行口语清洗：
    1. 去除“呃、啊、那个、就是说、然后然后”等语气词和无意义口头禅；
    2. 消除口吃重叠与自我修正半句；
    3. 【红线铁律】保留全部业务信息、人名、数字、金额、观点，绝对不篡改原意；
    4. 保持角色分离与时间戳严格对齐。
    """

    SYSTEM_PROMPT = """你是一个顶级商务与技术速记整理专家。你的任务是将 ASR 语音识别输出的原始口语转录稿，规整为流畅、规范、专业的书面谈话文稿。

【严格执行规则】：
1. 输入包含序号、说话人和原始口语。你必须对每条记录进行逐句规整，输出对应的纯正书面语。
2. 彻底清除语气词与口头禅（如：“呃”、“啊”、“那个”、“就是说”、“然后然后”、“对对对”等）。
3. 修正口语颠倒语序与口吃重复，使语句自然通顺。
4. 【最高红线 - 严禁信息失真】：
   - 必须 100% 完整保留原文中的所有业务事实、人名、职位、数字、金额、百分比、日期时间、专有名词、业务决策与正负观点。
   - 严禁自行补充未提及的内容！严禁改变说话人的原意与倾向！
5. 输出必须严格为 JSON 数组格式，不要包含任何额外的问候语或说明：
[
  {"seq_order": 1, "polished_text": "整理后的书面句子"}
]
"""

    BATCH_SIZE = 35

    @classmethod
    async def polish_segments(cls, segments: List[AsrSegmentData]) -> List[PolishedSegmentData]:
        if not segments:
            return []

        # For small recordings (<= 35 segments), process directly
        if len(segments) <= cls.BATCH_SIZE:
            return await cls._polish_single_batch(segments)

        # For long recordings (e.g. 1 hour with 500+ segments), split into batches
        batches = [segments[i : i + cls.BATCH_SIZE] for i in range(0, len(segments), cls.BATCH_SIZE)]
        logger.info("Splitting %d segments into %d batches for concurrent polishing", len(segments), len(batches))

        sem = asyncio.Semaphore(4)

        async def _safe_batch_polish(b: List[AsrSegmentData]) -> List[PolishedSegmentData]:
            async with sem:
                try:
                    return await cls._polish_single_batch(b)
                except Exception as err:
                    logger.warning("Batch polishing failed: %s, falling back to rule-based for this batch", err)
                    return cls._polish_with_rules(b)

        tasks = [_safe_batch_polish(b) for b in batches]
        results_nested = await asyncio.gather(*tasks)

        all_polished = []
        for r in results_nested:
            all_polished.extend(r)

        all_polished.sort(key=lambda x: x.seq_order)
        return all_polished

    @classmethod
    async def _polish_single_batch(cls, segments: List[AsrSegmentData]) -> List[PolishedSegmentData]:
        provider = (settings.DEFAULT_LLM_PROVIDER or "deepseek").lower()

        # Try preferred provider first
        if provider == "deepseek" and settings.DEEPSEEK_API_KEY:
            try:
                return await cls._polish_with_deepseek(segments)
            except Exception as e:
                logger.warning("DeepSeek 抛光失败: %s，尝试备选方案", e)
        elif provider == "gemini" and settings.GEMINI_API_KEY:
            try:
                return await cls._polish_with_gemini(segments)
            except Exception as e:
                logger.warning("Gemini 抛光失败: %s，尝试备选方案", e)
        elif provider == "dashscope" and settings.DASHSCOPE_API_KEY:
            try:
                return await cls._polish_with_dashscope(segments)
            except Exception as e:
                logger.warning("DashScope 抛光失败: %s，尝试备选方案", e)

        # Fallback to any available configured provider
        if settings.DEEPSEEK_API_KEY and provider != "deepseek":
            try:
                return await cls._polish_with_deepseek(segments)
            except Exception as e:
                logger.warning("DeepSeek 备选抛光失败: %s", e)
        elif settings.GEMINI_API_KEY and provider != "gemini":
            try:
                return await cls._polish_with_gemini(segments)
            except Exception as e:
                logger.warning("Gemini 备选抛光失败: %s", e)
        elif settings.DASHSCOPE_API_KEY and provider != "dashscope":
            try:
                return await cls._polish_with_dashscope(segments)
            except Exception as e:
                logger.warning("DashScope 备选抛光失败: %s", e)

        # Fallback heuristic / rule-based cleaning
        return cls._polish_with_rules(segments)

    @classmethod
    async def _polish_with_gemini(cls, segments: List[AsrSegmentData]) -> List[PolishedSegmentData]:
        url = f"{settings.GEMINI_BASE_URL}/v1beta/models/{settings.CLEANING_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"

        input_data = [
            {"seq_order": s.seq_order, "speaker": s.speaker_id, "raw_text": s.raw_text}
            for s in segments
        ]

        user_prompt = f"请规整以下 ASR 转录片段，严格遵循规则并以 JSON 数组返回：\n{json.dumps(input_data, ensure_ascii=False)}"

        payload = {
            "system_instruction": {"parts": [{"text": cls.SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json"
            }
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini API 报错 (HTTP {resp.status_code}): {resp.text}")

            res_json = resp.json()
            text_result = res_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "[]")
            parsed = json.loads(text_result.strip())

            # Map polished text by seq_order
            polished_map = {item.get("seq_order"): item.get("polished_text", "") for item in parsed}

            results = []
            for s in segments:
                cleaned_text = cls._fidelity_safe(s.raw_text, polished_map.get(s.seq_order))
                results.append(
                    PolishedSegmentData(
                        speaker_id=s.speaker_id,
                        start_ms=s.start_ms,
                        end_ms=s.end_ms,
                        polished_text=cleaned_text,
                        seq_order=s.seq_order
                    )
                )
            return results

    @classmethod
    async def _polish_with_deepseek(cls, segments: List[AsrSegmentData]) -> List[PolishedSegmentData]:
        url = f"{settings.DEEPSEEK_BASE_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }

        input_data = [
            {"seq_order": s.seq_order, "speaker": s.speaker_id, "raw_text": s.raw_text}
            for s in segments
        ]

        user_prompt = f"请规整以下 ASR 转录片段，严格遵循规则并以 JSON 数组返回：\n{json.dumps(input_data, ensure_ascii=False)}"

        payload = {
            "model": settings.DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": cls.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"}
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"DeepSeek API 报错: {resp.text}")

            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "[]")
            data = json.loads(content)
            if isinstance(data, dict):
                data = data.get("result") or data.get("segments") or list(data.values())[0]

            polished_map = {item.get("seq_order"): item.get("polished_text", "") for item in data if isinstance(item, dict)}

            results = []
            for s in segments:
                cleaned_text = cls._fidelity_safe(s.raw_text, polished_map.get(s.seq_order))
                results.append(
                    PolishedSegmentData(
                        speaker_id=s.speaker_id,
                        start_ms=s.start_ms,
                        end_ms=s.end_ms,
                        polished_text=cleaned_text,
                        seq_order=s.seq_order
                    )
                )
            return results

    @classmethod
    async def _polish_with_dashscope(cls, segments: List[AsrSegmentData]) -> List[PolishedSegmentData]:
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}",
            "Content-Type": "application/json"
        }

        input_data = [
            {"seq_order": s.seq_order, "speaker": s.speaker_id, "raw_text": s.raw_text}
            for s in segments
        ]

        user_prompt = f"请规整以下 ASR 转录片段，严格遵循规则并以 JSON 数组返回：\n{json.dumps(input_data, ensure_ascii=False)}"

        payload = {
            "model": "qwen-turbo",
            "messages": [
                {"role": "system", "content": cls.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"}
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"DashScope API 报错: {resp.text}")

            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "[]")
            # Parse json array or object
            data = json.loads(content)
            if isinstance(data, dict):
                data = data.get("result") or data.get("segments") or list(data.values())[0]

            polished_map = {item.get("seq_order"): item.get("polished_text", "") for item in data if isinstance(item, dict)}

            results = []
            for s in segments:
                cleaned_text = cls._fidelity_safe(s.raw_text, polished_map.get(s.seq_order))
                results.append(
                    PolishedSegmentData(
                        speaker_id=s.speaker_id,
                        start_ms=s.start_ms,
                        end_ms=s.end_ms,
                        polished_text=cleaned_text,
                        seq_order=s.seq_order
                    )
                )
            return results

    @classmethod
    def _extract_numbers(cls, text: str) -> list:
        """Extract numeric tokens (business-critical: amounts, counts, dates)."""
        return re.findall(r"\d+(?:[.,]\d+)*", text or "")

    @classmethod
    def _fidelity_safe(cls, raw_text: str, polished_text: str) -> str:
        """
        Post-polish fidelity guard. If the LLM dropped or altered any numeric
        token from the raw ASR text (e.g. '5000元' -> '5元'), fall back to the
        deterministic rule-based cleaned version which never invents content.
        """
        if not polished_text:
            return cls._clean_text_rules(raw_text)
        raw_nums = cls._extract_numbers(raw_text)
        polished_nums = cls._extract_numbers(polished_text)
        if raw_nums:
            from collections import Counter
            missing = Counter(raw_nums) - Counter(polished_nums)
            if missing:
                # Numeric distortion detected — revert to safe rule-based output
                return cls._clean_text_rules(raw_text)
        return polished_text

    @classmethod
    def _clean_text_rules(cls, text: str) -> str:
        """High-precision regex cleaner for Chinese disfluency removal"""
        # Remove common fillers at sentence beginning or standalone
        patterns = [
            r"^(呃|啊|那个|那么|就是说|然后然后|其实|你看)[，,\s]*",
            r"[，,\s]*(呃|啊|那个|然后然后)[，,\s]*",
            r"(对对对|是是是|好好好)",
            r"(\w)\1{2,}",  # triple repeat characters e.g. "这这这" -> "这"
        ]
        cleaned = text
        for p in patterns:
            cleaned = re.sub(p, "", cleaned)
        cleaned = re.sub(r"^[，,。！\s]+", "", cleaned)
        return cleaned.strip() or text

    @classmethod
    def _polish_with_rules(cls, segments: List[AsrSegmentData]) -> List[PolishedSegmentData]:
        results = []
        for s in segments:
            results.append(
                PolishedSegmentData(
                    speaker_id=s.speaker_id,
                    start_ms=s.start_ms,
                    end_ms=s.end_ms,
                    polished_text=cls._clean_text_rules(s.raw_text),
                    seq_order=s.seq_order
                )
            )
        return results
