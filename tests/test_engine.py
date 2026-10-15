"""엔진 수명주기·로컬 전용·토큰 길이 경계 (Codex 검토 지적사항)."""

import numpy as np
import pytest

from conftest import requires_engine
from noseout import engine, prompts

pytestmark = requires_engine


def test_special_tokens(embedder):
    tokens = embedder._tokenize(prompts.query("hello"))
    bos = embedder._tokenize("")[0]
    assert tokens[0] == bos and tokens[-1] == embedder.eos
    assert tokens.count(bos) == 1 and tokens.count(embedder.eos) == 1


def test_token_ids_give_same_vector_as_text(embedder):
    text = prompts.document("The lease allows one cat or one small dog with a deposit.", title="Lease")
    by_ids = embedder.embed_texts([text])[0]
    by_text = embedder._embed([text])[0]
    assert float(np.dot(by_ids, by_text)) > 0.99999


@pytest.mark.parametrize("unit", ["Note about the projector. ", "프로젝터 고장에 관한 메모입니다. "], ids=["en", "ko"])
def test_length_boundary(embedder, unit):
    base = len(embedder._tokenize(prompts.document("")))
    per = len(embedder._tokenize(prompts.document(unit * 50))) - base
    for target in (2000, 2048, 3000, 6000):
        text = prompts.document(unit * max(1, int((target - base) / per * 50)))
        raw = embedder._tokenize(text)
        cut = embedder.tokens(text)
        assert len(cut) == min(len(raw), engine.MAX_TOKENS)
        assert cut[-1] == embedder.eos and cut[:-1] == raw[: len(cut) - 1]
        vector = embedder.embed_texts([text])[0]
        assert vector.shape == (768,) and np.isfinite(vector).all()


def test_overlong_equals_its_cut(embedder):
    unit = "서울역에서 부산으로 가는 기차는 오후 두 시에 출발합니다. "
    long = prompts.document(unit * 400)
    short = prompts.document(unit * 401)  # 더 길어도 앞 2048 토큰이 같으면 벡터도 같아야 한다
    a, b = embedder.embed_texts([long, short])
    assert float(np.dot(a, b)) > 0.99999


def test_proxy_environment_is_ignored(monkeypatch):
    # 죽은 프록시를 지정해도 127.0.0.1 서버와 직접 통신해야 한다
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with engine.Embedder(engine.ServerConfig(media_model=None)) as e:
        assert e.embed_texts([prompts.query("x")]).shape == (1, 768)


def test_failed_startup_leaves_no_server(monkeypatch):
    started = []
    original = engine.subprocess.Popen

    def spy(*args, **kwargs):
        started.append(original(*args, **kwargs))
        return started[-1]

    def stall(self, timeout):
        raise TimeoutError("simulated stalled /health")

    monkeypatch.setattr(engine.subprocess, "Popen", spy)
    monkeypatch.setattr(engine.Embedder, "_wait_ready", stall)
    with pytest.raises(TimeoutError):
        engine.Embedder(engine.ServerConfig(media_model=None))
    assert started and started[0].poll() is not None


def test_bad_model_fails_cleanly(tmp_path):
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"not a model")
    with pytest.raises(RuntimeError):
        engine.Embedder(engine.ServerConfig(text_model=bad, media_model=None), startup_timeout=60)


def test_cancelled_startup_leaves_no_server(monkeypatch):
    started = []
    original = engine.subprocess.Popen

    def spy(*args, **kwargs):
        started.append(original(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(engine.subprocess, "Popen", spy)
    with pytest.raises(RuntimeError, match="startup stopped"):
        engine.Embedder(engine.ServerConfig(media_model=None), should_stop=lambda: True)
    assert started and started[0].poll() is not None


@pytest.mark.parametrize("forced", [False, True], ids=["abrupt-exit", "parent-killed"])
def test_parent_crash_only_stops_its_own_server(tmp_path, embedder, forced):
    """별도 부모를 비정상 종료시켜 소유 서버의 종료와 무관한 엔진의 생존을 동시에 확인한다."""
    import ctypes
    import os
    import subprocess
    import sys
    import time
    from ctypes import wintypes

    pid_file = tmp_path / "server.pid"
    code = ("import os, sys\nfrom pathlib import Path\nfrom noseout.engine import Embedder, ServerConfig\n"
            "e = Embedder(ServerConfig(media_model=None))\n"
            "Path(sys.argv[1]).write_text(str(e.process.pid))\n"
            "sys.stdin.read(1)\nos._exit(23)\n")
    parent = subprocess.Popen([sys.executable, "-c", code, str(pid_file)], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              env={**os.environ, "PYTHONPATH": str(engine.ROOT / "src")})
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        deadline = time.monotonic() + 60
        while not pid_file.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), parent.stderr.read() if parent.poll() is not None else "startup timeout"
        # PID를 찾은 직후 핸들을 잡아 재사용된 PID를 잘못 판정하지 않는다.
        handle = api.OpenProcess(0x100000, False, int(pid_file.read_text()))
        assert handle
        if forced:
            parent.kill()
            parent.communicate(timeout=10)
        else:
            parent.communicate(input=b"x", timeout=10)
            assert parent.returncode == 23
        assert api.WaitForSingleObject(handle, 10000) == 0
        assert embedder.process.poll() is None
        assert embedder.embed_texts([prompts.query("still alive")]).shape == (1, 768)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.communicate(timeout=10)
        if handle:
            api.CloseHandle(handle)
