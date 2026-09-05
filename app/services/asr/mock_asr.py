import asyncio
from typing import List
from app.services.asr.base import BaseAsrProvider, AsrSegmentData


class MockAsrProvider(BaseAsrProvider):
    """
    测试 / 演示模式 ASR Provider
    当用户尚未配置外部 API Key 时，提供逼真的多说话人、带语气词逐字稿进行全流程验证。
    """
    async def transcribe(self, audio_file_path: str, recording_id: str) -> List[AsrSegmentData]:
        # Simulate slight network delay
        await asyncio.sleep(2)

        sample_dialogues = [
            ("说话人 1", 0, 5200, "呃...那个，大家上午好，我们今天主要就是对一下这个智能语音系统的技术选型和交付节点。"),
            ("说话人 2", 5500, 11800, "对对对，李总，我先汇报一下。目前我们初步评估了火山引擎豆包和阿里通义两个ASR方案，豆包在中文口语上的识别率大概在96.8%左右。"),
            ("说话人 1", 12100, 18400, "嗯...那个成本方面呢？比如我们每天大概处理50个小时的音频，算下来月度预算大概是多少？"),
            ("说话人 2", 18700, 26300, "成本非常低，按量付费的话每个小时大概在1块2毛钱，一个月50小时乘30天也就大概1800元，完全在之前批复的5000元预算额度内。"),
            ("说话人 3", 26800, 34500, "然后然后...那个关于服务器部署，我们现在有一台1核1G内存的新加坡机器，建议不要跑笨重的全家桶，用FastAPI加轻量队列最稳妥。"),
            ("说话人 1", 35000, 42200, "好！那这个就这么定了。王工负责在周五前把数据库和双轨存储调通，张工负责对接豆包和千问的API预留，下周一我们准时上线内测版。"),
            ("说话人 2", 42500, 47000, "收到，周五下午下班前我把接口联调报告发到群里。"),
            ("说话人 3", 47200, 51000, "没问题，我这边同步搞定前端的音频波形联动。"),
        ]

        results = []
        for idx, (spk, start_ms, end_ms, text) in enumerate(sample_dialogues):
            results.append(
                AsrSegmentData(
                    speaker_id=spk,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    raw_text=text,
                    seq_order=idx + 1
                )
            )
        return results
