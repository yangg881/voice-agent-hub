from abc import ABC, abstractmethod
from typing import List
from pydantic import BaseModel


class AsrSegmentData(BaseModel):
    speaker_id: str
    start_ms: int
    end_ms: int
    raw_text: str
    seq_order: int


class BaseAsrProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio_file_path: str, recording_id: str, **kwargs) -> List[AsrSegmentData]:
        """Transcribe audio and return segments with speakers and timestamps"""
        pass


SegmentData = AsrSegmentData

