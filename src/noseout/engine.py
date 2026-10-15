"""EmbeddingGemma 2 추론 엔진: llama.cpp의 llama-server(CUDA)를 127.0.0.1에만 띄우고 HTTP로 벡터를 받는다.

원본 DigUp은 llama.cpp를 라이브러리로 직접 링크하지만(LlamaEmbedder.swift), Windows용 Python 버전에서는 공식
릴리스의 CUDA 빌드를 그대로 쓰기 위해 서버 프로세스로 돌린다. 원본과 같은 점:
- 같은 llama.cpp 태그(b11461), 같은 GGUF(Q8_0, 같은 리비전), mean pooling, L2 정규화
- 한 입력이 한 micro-batch에 들어가도록 batch/ubatch를 크게 잡는다. 서버는 텍스트와 미디어를 한 ubatch에서
  처리하면서 이미지 토큰을 ubatch의 절반으로 제한하므로, 1120 토큰 이미지를 위해 4096으로 둔다.
- 이미지는 미리 기준 구현과 같은 크기로 줄이고(image_sizing), mtmd는 그대로 받게 한다
- 음성은 앞 30초만 쓴다
"""

from __future__ import annotations

import base64
import ctypes
import io
import errno
import os
import socket
import subprocess
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import requests

from . import image_sizing

ROOT = Path(__file__).resolve().parents[2]
SERVER_EXE = ROOT / "vendor" / "llama" / "llama-server.exe"
TEXT_MODEL = ROOT / "models" / "embeddinggemma-2-Q8_0.gguf"
MEDIA_MODEL = ROOT / "models" / "mmproj-embeddinggemma-2-Q8_0.gguf"
MEDIA_MARKER = "<__media__>"  # mtmd_default_marker()
MAX_TOKENS = 2048  # 텍스트 한 입력의 상한 (원본과 같음)
UBATCH = 4096
AUDIO_SECONDS = 30
DIM = 768


@dataclass
class ServerConfig:
    exe: Path = SERVER_EXE
    text_model: Path = TEXT_MODEL
    media_model: Path | None = MEDIA_MODEL
    gpu_layers: int = 999  # 0이면 CPU 전용
    threads: int = min(os.cpu_count() or 4, 8)
    log: Path | None = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerJob:
    """Windows가 부모의 비정상 종료에도 이 엔진만 정리하게 한다. 이름·PID로 다른 서버를 찾아 끄지 않는다."""

    def __init__(self):
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)  # 상속하지 않는 무명 핸들: 앱이 죽으면 마지막 핸들이 닫힌다
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (Microsoft Job Objects)
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class Embedder:
    """llama-server 하나를 소유하는 임베더. `with Embedder() as e:` 로 쓰면 끝날 때 서버를 종료한다."""

    def __init__(self, config: ServerConfig | None = None, startup_timeout: float = 120,
                 should_stop: Callable[[], bool] | None = None):
        self.config = config or ServerConfig()
        # 실행 파일·모델이 없으면 시작하기 전에 어느 파일인지 알린다. 폴더째 없으면 Popen은 파일 이름 없이 WinError 2를
        # 내서 화면이 "예상하지 못한 오류"만 보였다(6단계 최종 점검에서 재현).
        for required in (self.config.exe, self.config.text_model, self.config.media_model):
            if required is not None and not Path(required).exists():
                raise FileNotFoundError(errno.ENOENT, "engine file not found", str(required))
        self._should_stop = should_stop
        self._close_lock = threading.Lock()
        self.process = None
        self._job = None
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        args = [
            str(self.config.exe),
            "-m", str(self.config.text_model),
            "--embeddings", "--pooling", "mean",
            "--host", "127.0.0.1", "--port", str(self.port),
            "-c", str(UBATCH), "-b", str(UBATCH), "-ub", str(UBATCH), "-np", "1",
            "-ngl", str(self.config.gpu_layers), "-t", str(self.config.threads),
            "--no-webui",
        ]
        if self.config.media_model:
            args += ["--mmproj", str(self.config.media_model),
                     "--image-min-tokens", "1", "--image-max-tokens", "1120"]
            if self.config.gpu_layers == 0:
                args.append("--no-mmproj-offload")
        # 환경변수의 프록시 설정(HTTP_PROXY 등)을 무시한다: 파일 내용이 127.0.0.1 밖으로 나가면 안 된다.
        self.http = requests.Session()
        self.http.trust_env = False
        self._log = open(self.config.log, "ab") if self.config.log else None
        # 서버는 실행마다 무작위 media marker를 쓴다: 프롬프트에 넣을 수 있게 고정한다.
        env = {**os.environ, "LLAMA_MEDIA_MARKER": MEDIA_MARKER}
        try:
            if os.name == "nt":
                self._job = _ServerJob()
            self.process = subprocess.Popen(
                args, stdout=self._log or subprocess.DEVNULL, stderr=self._log or subprocess.DEVNULL,
                cwd=self.config.exe.parent, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if self._job:
                self._job.assign(self.process)
            self.load_seconds = self._wait_ready(startup_timeout)
            self.eos = self._tokenize("")[-1]  # add_special=True면 <bos> … <eos>
        except BaseException:
            self.close()
            raise

    def _wait_ready(self, timeout: float) -> float:
        started = time.perf_counter()
        while time.perf_counter() - started < timeout:
            if self._should_stop and self._should_stop():
                raise RuntimeError("llama-server startup stopped")
            if self.process.poll() is not None:
                raise RuntimeError(f"llama-server exited with {self.process.returncode}")
            try:
                if self.http.get(f"{self.url}/health", timeout=2).status_code == 200:
                    return time.perf_counter() - started
            except requests.RequestException:
                pass  # 아직 뜨는 중, 또는 응답이 늦음: 기한 안에서 다시 묻는다
            time.sleep(0.25)
        raise TimeoutError("llama-server did not become ready")

    def close(self) -> None:
        # 종료 버튼과 작업 스레드의 finally가 겹쳐도 같은 핸들을 두 번 닫지 않는다.
        with self._close_lock:
            if self._job:
                self._job.close()
                self._job = None
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(10)
            self.http.close()
            if self._log:
                self._log.close()
                self._log = None

    def __enter__(self) -> "Embedder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # 임베딩

    def _embed(self, content) -> np.ndarray:
        reply = self.http.post(f"{self.url}/embedding", json={"content": content, "embd_normalize": 2}, timeout=300)
        reply.raise_for_status()
        rows = reply.json()
        vectors = []
        for row in sorted(rows, key=lambda r: r["index"]):
            embedding = np.asarray(row["embedding"], dtype=np.float32)
            vectors.append(embedding.reshape(-1, embedding.shape[-1])[0])  # pooled: 한 행
        out = np.stack(vectors)
        return out / np.linalg.norm(out, axis=1, keepdims=True)

    def _tokenize(self, text: str) -> list[int]:
        reply = self.http.post(f"{self.url}/tokenize",
                               json={"content": text, "add_special": True, "parse_special": True}, timeout=60)
        reply.raise_for_status()
        return reply.json()["tokens"]

    def tokens(self, text: str) -> list[int]:
        """`<bos> text <eos>`, MAX_TOKENS를 넘으면 원본(transformers)처럼 앞부분 + `<eos>`로 자른다."""
        tokens = self._tokenize(text)
        if len(tokens) > MAX_TOKENS:
            tokens = tokens[: MAX_TOKENS - 1] + [self.eos]
        return tokens

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """프롬프트가 이미 붙은 텍스트들 → (N, 768). 토큰 id로 보내므로 길이 상한이 여기서 정해진다."""
        return self._embed([self.tokens(text) for text in texts])

    def embed_image(self, path: str, budget: int = 280) -> np.ndarray:
        png = image_sizing.prepare(path, budget)
        return self._media(png)

    def embed_audio(self, path: str) -> np.ndarray:
        return self._media(_first_seconds(path, AUDIO_SECONDS))

    def _media(self, data: bytes) -> np.ndarray:
        payload = {"prompt_string": MEDIA_MARKER, "multimodal_data": [base64.b64encode(data).decode("ascii")]}
        return self._embed(payload)[0]


def _first_seconds(path: str, seconds: int) -> bytes:
    """WAV의 앞 `seconds`초. WAV가 아닌 형식은 지금은 그대로 보낸다(30초 자르기는 이후 ffmpeg 단계에서)."""
    data = Path(path).read_bytes()
    try:
        with wave.open(io.BytesIO(data)) as source:
            limit = seconds * source.getframerate()
            if source.getnframes() <= limit:
                return data
            params = source.getparams()
            frames = source.readframes(limit)
    except wave.Error:
        return data
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setparams(params)
        target.writeframes(frames)
    return out.getvalue()


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
