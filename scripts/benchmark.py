"""엔진 속도·VRAM 측정. 합성 입력(tests/reference, tests/fixtures)만 쓴다.

usage: .venv\\Scripts\\python scripts\\benchmark.py [--cpu]
"""

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from noseout import engine, prompts  # noqa: E402

REF = ROOT / "tests" / "reference"


def vram_mib(pid):
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out.splitlines()[0]) if out else None


def timed(fn, repeat):
    fn()  # 워밍업
    started = time.perf_counter()
    for _ in range(repeat):
        out = fn()
    return (time.perf_counter() - started) / repeat, out


def main():
    cpu = "--cpu" in sys.argv
    before = vram_mib(None)
    config = engine.ServerConfig(gpu_layers=0 if cpu else 999, log=ROOT / "results" / "benchmark-server.log")
    results = {"mode": "cpu" if cpu else "cuda"}
    with engine.Embedder(config) as e:
        results["load_seconds"] = round(e.load_seconds, 2)
        results["vram_delta_mib"] = (vram_mib(None) - before) if before is not None and not cpu else None
        query = [prompts.query("payment declined error")]
        results["query_ms"] = round(timed(lambda: e.embed_texts(query), 20)[0] * 1000, 1)
        docs = [prompts.document(f"Paragraph {i}: " + "the quarterly report covers revenue, costs and hiring. " * 20)
                for i in range(32)]
        seconds, _ = timed(lambda: e.embed_texts(docs), 3)
        results["docs_per_second_~250tok"] = round(len(docs) / seconds, 1)
        for budget in (70, 280, 1120):
            seconds, v = timed(lambda: e.embed_image(str(REF / "photo.jpg"), budget), 5)
            results[f"image_{budget}_ms"] = round(seconds * 1000, 1)
        seconds, _ = timed(lambda: e.embed_audio(str(REF / "speech.wav")), 5)
        results["audio_4s_ms"] = round(seconds * 1000, 1)
        if not cpu:
            results["vram_peak_mib_after_work"] = vram_mib(None) - before if before is not None else None
        # 1120 예산이 실제로 다른(더 세밀한) 벡터를 내는지
        v70, v1120 = e.embed_image(str(REF / "photo.jpg"), 70), e.embed_image(str(REF / "photo.jpg"), 1120)
        results["photo_cos_70_vs_1120"] = round(float(np.dot(v70, v1120)), 4)
    out = ROOT / "results" / f"benchmark-{results['mode']}.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
