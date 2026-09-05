import os
import shutil
import subprocess
import json
from pathlib import Path
from typing import Tuple, Dict, Any
from app.config import PROCESSED_AUDIO_DIR


class AudioService:
    @staticmethod
    def is_ffmpeg_available() -> bool:
        return shutil.which("ffmpeg") is not None

    @staticmethod
    def is_ffprobe_available() -> bool:
        return shutil.which("ffprobe") is not None

    @classmethod
    def get_audio_info(cls, file_path: str) -> Dict[str, Any]:
        """Extract duration and metadata using ffprobe"""
        if not cls.is_ffprobe_available():
            # Fallback estimation based on file size if ffprobe is not yet installed
            size = os.path.getsize(file_path)
            return {"duration": 60.0, "format": "unknown", "size": size}

        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path,
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            data = json.loads(res.stdout)
            duration = float(data.get("format", {}).get("duration", 0.0))
            return {
                "duration": duration,
                "format": data.get("format", {}).get("format_name", ""),
                "size": int(data.get("format", {}).get("size", os.path.getsize(file_path))),
            }
        except Exception as e:
            size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            return {"duration": 0.0, "format": "unknown", "size": size, "error": str(e)}

    @classmethod
    def process_and_normalize(cls, raw_path: str, recording_id: str) -> Tuple[str, float]:
        """
        Transcodes raw audio (mp3, m4a, wav, amr, silk, ogg, webm, etc.) to
        standardized 16kHz mono MP3 for optimal ASR accuracy and light streaming.
        Returns (processed_file_path, duration_seconds).
        """
        output_filename = f"{recording_id}_processed.mp3"
        output_path = str(PROCESSED_AUDIO_DIR / output_filename)

        if not cls.is_ffmpeg_available():
            # If ffmpeg is somehow missing, copy original file directly
            shutil.copyfile(raw_path, output_path)
            info = cls.get_audio_info(output_path)
            return output_path, info.get("duration", 0.0)

        # High-performance voice-optimized ffmpeg filter chain:
        # 1. highpass at 80Hz (removes low rumble / wind / table vibration)
        # 2. lowpass at 7500Hz (human speech range)
        # 3. dynaudnorm (dynamic audio normalizer to balance loud and quiet voices)
        # 4. 16kHz mono audio (ideal for ASR engines)
        cmd = [
            "ffmpeg",
            "-y",
            "-i", raw_path,
            "-af", "highpass=f=80,lowpass=f=7500,dynaudnorm=f=150:g=15",
            "-ar", "16000",
            "-ac", "1",
            "-b:a", "64k",
            output_path,
        ]

        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError:
            # Fallback without audio filters if raw audio codec is quirky
            fallback_cmd = [
                "ffmpeg",
                "-y",
                "-i", raw_path,
                "-ar", "16000",
                "-ac", "1",
                output_path,
            ]
            subprocess.run(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

        info = cls.get_audio_info(output_path)
        duration = info.get("duration", 0.0)
        return output_path, duration

    @classmethod
    def convert_to_wav(cls, input_path: str, output_path: str) -> float:
        """
        Converts short voice recording (webm/mp4/aac/ogg) into standardized
        16kHz mono 16-bit PCM WAV for fast ASR transcription.
        Returns duration in seconds.
        """
        if os.path.abspath(input_path) == os.path.abspath(output_path):
            info = cls.get_audio_info(output_path)
            return float(info.get("duration", 0.0))

        if not cls.is_ffmpeg_available():
            shutil.copyfile(input_path, output_path)
            return cls.get_audio_info(output_path).get("duration", 0.0)


        cmd = [
            "ffmpeg",
            "-y",
            "-i", input_path,
            "-ar", "16000",
            "-ac", "1",
            "-acodec", "pcm_s16le",
            output_path,
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except Exception as e:
            # If failed, copy directly
            shutil.copyfile(input_path, output_path)

        info = cls.get_audio_info(output_path)
        return float(info.get("duration", 0.0))

    @classmethod
    def concat_audio_files(cls, input_files: list, output_path: str) -> float:
        """
        Concatenates multiple audio segment files into a single master audio track using ffmpeg concat demuxer.
        Returns total duration in seconds.
        """
        if not input_files:
            return 0.0

        if len(input_files) == 1:
            shutil.copyfile(input_files[0], output_path)
            return cls.get_audio_info(output_path).get("duration", 0.0)

        if not cls.is_ffmpeg_available():
            shutil.copyfile(input_files[0], output_path)
            return cls.get_audio_info(output_path).get("duration", 0.0)

        # Create temporary concat list file
        concat_txt = Path(output_path).parent / f"concat_{os.path.basename(output_path)}.txt"
        with open(concat_txt, "w", encoding="utf-8") as f:
            for item in input_files:
                # ffmpeg requires paths with escaped backslashes or forward slashes
                clean_path = str(Path(item).resolve()).replace("\\", "/")
                f.write(f"file '{clean_path}'\n")

        cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_txt),
            "-ar", "16000",
            "-ac", "1",
            "-b:a", "64k",
            output_path,
        ]

        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        finally:
            if concat_txt.exists():
                try:
                    concat_txt.unlink()
                except Exception:
                    pass

        info = cls.get_audio_info(output_path)
        return float(info.get("duration", 0.0))
