"""음성·영상: ffmpeg로 디코딩하고 ffprobe로 잰다. 원본 Platform/Portable/Media+Portable.swift와 MediaExtractors.swift를 옮겼다.

- 음성: 16 kHz mono로 풀어 30초 창을 25초 간격으로 WAV로 쓴다(모델은 한 입력에서 30초 넘는 소리를 버린다). 거의 무음인
  창(RMS ≤ 0.003, 약 −50 dBFS)은 건너뛰고, 마지막 꼬리는 2초 이상 새 소리가 있을 때만. 한 번에 한 창만 메모리에 둔다.
- 영상: 3초마다 프레임 하나(긴 영상은 최대 1,200장이 되게 간격을 늘림), 긴 변 1024px. 앞 프레임과 8×8 평균 해시가
  4비트 이내로 같으면 같은 장면으로 보고 버린다. 남은 프레임은 다음 프레임까지의 구간을 대표한다.
ffmpeg는 vendor/ffmpeg/*/bin(프로젝트 안), $NOSEOUT_FFMPEG, PATH 순으로 찾는다. 사용자 파일은 읽기만 한다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .extractors import ExtractError

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_RATE = 16_000
AUDIO_WINDOW = 30.0
AUDIO_HOP = 25.0
MIN_TAIL = 2.0
SILENCE_RMS = 0.003
FRAME_INTERVAL = 3.0
FRAME_PIXELS = 1024
MAX_FRAMES = 1200
HASH_BITS_SAME = 4
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Piece:
    path: str
    start: float
    end: float


def find(tool: str) -> str | None:
    # 옛 이름 DIGUPWIN_FFMPEG도 받는다(2026-10-15 NoseOut 이름 통일 전 설정 호환)
    chosen = os.environ.get("NOSEOUT_FFMPEG") or os.environ.get("DIGUPWIN_FFMPEG")
    folders = [Path(chosen)] if chosen else []
    folders += sorted((ROOT / "vendor" / "ffmpeg").glob("*/bin"))
    for folder in folders:
        candidate = folder / f"{tool}.exe"
        if candidate.exists():
            return str(candidate)
    return shutil.which(tool)


def _tool(name: str) -> str:
    path = find(name)
    if not path:
        raise ExtractError(f"{name} isn't installed (audio and video need it)")
    return path


def probe(path: str) -> tuple[float, bool, bool]:
    """(길이 초, 소리 있음, 영상 있음). 그림 한 장짜리 '영상'(앨범 표지)은 영상으로 치지 않는다."""
    result = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=codec_type:stream_disposition=attached_pic",
         "-of", "default=noprint_wrappers=1", path],
        capture_output=True, text=True, creationflags=_NO_WINDOW)
    if result.returncode != 0:
        raise ExtractError(f"unreadable media: {result.stderr.strip()[:200]}")
    duration, audio, video, attached = 0.0, False, False, False
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "duration":
            try:
                duration = float(value)
            except ValueError:
                pass
        elif key == "codec_type":
            audio |= value == "audio"
            video |= value == "video"
        elif key == "DISPOSITION:attached_pic" and value == "1":
            attached = True
    return (duration if np.isfinite(duration) and duration > 0 else 0.0), audio, video and not attached


def audio_windows(path: str, folder: Path, window: float = AUDIO_WINDOW, hop: float = AUDIO_HOP) -> tuple[list[Piece], float]:
    """(WAV 조각들, 길이 초). 소리가 없으면 빈 목록."""
    duration, has_audio, _ = probe(path)
    if not has_audio:
        return [], duration
    window_n, hop_n = int(window * SAMPLE_RATE), int(hop * SAMPLE_RATE)
    pieces: list[Piece] = []
    buffer = np.zeros(0, np.int16)
    offset = 0
    leftover = b""

    def emit(samples: np.ndarray) -> None:
        if rms(samples) <= SILENCE_RMS:
            return
        out = folder / f"{uuid.uuid4().hex}.wav"
        write_wav(samples, out)
        pieces.append(Piece(str(out), offset / SAMPLE_RATE, (offset + len(samples)) / SAMPLE_RATE))

    process = subprocess.Popen(
        [_tool("ffmpeg"), "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    try:
        while chunk := process.stdout.read(1 << 16):
            data = leftover + chunk
            usable = len(data) // 2 * 2
            leftover = data[usable:]
            buffer = np.concatenate([buffer, np.frombuffer(data[:usable], "<i2")])
            while len(buffer) >= window_n:
                emit(buffer[:window_n])
                buffer = buffer[hop_n:]
                offset += hop_n
    finally:
        process.stdout.close()
        code = process.wait()
    if code != 0:
        raise ExtractError(f"ffmpeg failed ({code}) on audio")
    covered = 0 if offset == 0 else window_n - hop_n   # 앞 창과 겹치는 부분
    if len(buffer) - covered >= int(MIN_TAIL * SAMPLE_RATE):
        emit(buffer)
    return pieces, duration or (offset + len(buffer)) / SAMPLE_RATE


def keyframes(path: str, folder: Path, interval: float = FRAME_INTERVAL) -> tuple[list[Piece], float, bool]:
    """(프레임 JPEG 조각들, 길이 초, 소리 있음)."""
    duration, has_audio, has_video = probe(path)
    if duration <= 0 or not has_video:
        return [], duration, has_audio
    step = max(interval, duration / MAX_FRAMES)
    first = min(0.5, duration / 2)
    prefix = f"frame-{uuid.uuid4().hex}-"
    scale = f"fps={1 / step},scale={FRAME_PIXELS}:{FRAME_PIXELS}:force_original_aspect_ratio=decrease"
    result = subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-nostdin", "-ss", str(first), "-i", path, "-an", "-vf", scale,
         "-q:v", "3", "-f", "image2", str(folder / f"{prefix}%06d.jpg")],
        capture_output=True, creationflags=_NO_WINDOW)
    if result.returncode != 0:
        raise ExtractError(f"ffmpeg failed ({result.returncode}) on video frames")
    frames: list[Piece] = []
    last_hash = None
    for index, name in enumerate(sorted(p.name for p in folder.glob(f"{prefix}*.jpg"))):
        frame = folder / name
        current = average_hash(frame)
        if last_hash is not None and bin(current ^ last_hash).count("1") <= HASH_BITS_SAME:
            frame.unlink()
            continue
        last_hash = current
        frames.append(Piece(str(frame), first + index * step, duration))
    for a, b in zip(frames, frames[1:]):
        a.end = b.start
    return frames, duration, has_audio


def average_hash(path: Path) -> int:
    """8×8 평균 해시: 칸의 회색값이 전체 평균보다 밝으면 1 (원본 RGBImage.averageHash)."""
    with Image.open(path) as image:
        gray = np.asarray(image.convert("L").resize((8, 8), Image.Resampling.BOX), np.float64)
    bits = (gray > gray.mean()).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def rms(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return 0.0
    values = samples.astype(np.float64) / 32768
    return float(np.sqrt(np.mean(values * values)))


def write_wav(samples: np.ndarray, path: Path) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(samples.astype("<i2").tobytes())
