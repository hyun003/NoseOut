"""U1 실험: 로컬 받아쓰기(whisper.cpp)로 음성 검색이 나아지는지 잰다. 제품 코드에는 연결하지 않는다.

비교: (A) 지금 방식 = 음성 벡터만, (B) 음성 벡터 + 받아쓰기 텍스트 구간(30초 음성 창과 같은 시점, 키워드 + 문서 벡터),
(C) B와 같되 전사 벡터를 자기 모달("transcript")로 분류. 점수식은 그대로다: 원본 규칙대로 모달별 평균을 빼며, 모달 행이
20개 미만이면 전체 평균을 쓴다. 1차 실행에서 짧은 전사 문장이 어떤 질의와도 코사인이 높아(평균 0.590 vs 문서 0.556) 문서
질의에 끼어드는 것을 확인해 C를 더했다. C가 효과를 보려면 전사 행이 20개 이상이어야 해서, 질의 없는 방해 음성 메모 20개를 더했다.
받아쓰기 결과는 손대지 않고 그대로 쓴다. 정답 문장을 넣지 않는다. 전사 벡터의 제목은 비운다(파일 이름이 단서가 되지 않게).
검색 점수식·setsScale은 제품 그대로(Searcher). 실패한 질의도 모두 기록한다.

입력: test_data(기존 합성), test_data_holdout(승인된 독립 평가, 음성 없음 → 방해 파일), 이 스크립트가 .tmp에 새로 만드는
독립 합성 음성 세트(질의는 받아쓰기 전에 아래에 고정). 색인·전사·임시 파일은 모두 .tmp/asr-eval에만 쓴다.
결과: results/u1-asr.json

    .venv\\Scripts\\python scripts\\eval_asr.py
"""

from __future__ import annotations

import ctypes
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import PySide6.QtCore  # noqa: E402,F401  (winrt보다 먼저)

import make_test_data  # noqa: E402  (TTS 함수와 팟캐스트 원문)
from noseout import engine, media, prompts, search_text  # noqa: E402
from noseout.indexer import Indexer  # noqa: E402
from noseout.searcher import Searcher  # noqa: E402
from noseout.store import IndexStore  # noqa: E402

WORK = ROOT / ".tmp" / "asr-eval"
WHISPER = ROOT / "vendor" / "whisper" / "Release" / "whisper-cli.exe"
MODEL = ROOT / "models" / "ggml-large-v3-turbo-q5_0.bin"
TEST_DATA, HOLDOUT = ROOT / "test_data", ROOT / "test_data_holdout"

MEMO_TEXT = "내일 오전 열 시에 치과 예약이 있어요. 잊지 말고 건강보험증을 챙겨 가세요."
NARRATION = ("Good evening. Tomorrow will be cloudy in the morning with light rain in the afternoon, "
             "so take a jacket if you go out.")
PODCAST_TEXT = " ".join(" ".join(sentences) for _, sentences in make_test_data.PODCAST)

# 기존 음성·영상 질의 (tests/test_media_search.py와 같음): 악화되지 않는지
MEDIA_QUERIES = [
    ("where they talk about sleep", "test_data/media/home_show_ep12.m4a", (25, 31.6), "audio"),
    ("tips for falling asleep at night", "test_data/media/home_show_ep12.m4a", (25, 31.6), "audio"),
    ("community garden opening", "test_data/media/home_show_ep12.m4a", (0, 13.7), "audio"),
    ("trains are faster than flying", "test_data/media/home_show_ep12.m4a", (25, 52.1), "audio"),
    ("black and white stripes in a video", "test_data/media/clip_2026-08-30.mp4", (0, 5), "frame"),
    ("a sailboat on the water", "test_data/media/clip_2026-08-30.mp4", (5, 10), "frame"),
    ("birthday cake with candles", "test_data/media/clip_2026-08-30.mp4", (9, 15), "frame"),
    ("weather forecast with rain tomorrow", "test_data/media/clip_2026-08-30.mp4", (0, 0), "soundtrack"),
]
MEMO = "test_data/media/음성메모_0917.m4a"
U1_QUERIES = [
    ("dentist appointment reminder voice note", "en"), ("dentist appointment tomorrow morning", "en"),
    ("remember to bring the health insurance card", "en"), ("voice memo about going to the dentist", "en"),
    ("치과 예약 음성 메모", "ko"), ("내일 아침 치과 예약", "ko"), ("건강보험증 챙기기", "ko"), ("치과 가는 것에 대한 음성 메모", "ko"),
]
MEMO_KEYWORDS = ["치과", "예약", "건강보험증", "오전", "챙겨"]

# 독립 합성 음성 세트: 받아쓰기를 보기 전에 정한 원문·질의. (파일, TTS 언어, 원문, [(질의, 언어)])
INDEPENDENT = [
    ("메모_자동차검사.m4a", "ko-KR", "다음 주 화요일에 자동차 정기 검사를 받아야 해요. 차량 등록증을 꼭 가져가세요.",
     [("car inspection reminder", "en"), ("bring the vehicle registration", "en"), ("자동차 정기 검사", "ko")]),
    ("메모_생신선물.m4a", "ko-KR", "엄마 생신 선물로 꽃다발과 케이크를 주문했어요. 토요일 오후 세 시에 찾으러 가야 해요.",
     [("pick up the flowers and cake for mom's birthday", "en"), ("생신 선물 케이크 주문", "ko")]),
    ("메모_관리비.m4a", "ko-KR", "관리비 고지서가 나왔는데 이번 달 전기 요금이 많이 올랐어요.",
     [("electricity bill went up this month", "en"), ("관리비 전기 요금", "ko")]),
    ("메모_처방전.m4a", "ko-KR", "약국에서 감기약을 사고 처방전은 책상 서랍에 넣어 두었어요.",
     [("where I put the prescription", "en"), ("처방전 보관한 곳", "ko")]),
    ("memo_passport.m4a", "en-US", "Remember to renew the passport before the trip to Lisbon in March.",
     [("passport renewal before travel", "en"), ("여권 갱신 알림", "ko")]),
    ("memo_plumber.m4a", "en-US", "The plumber is coming on Thursday morning to fix the leak under the kitchen sink.",
     [("plumber appointment for the leaking sink", "en"), ("싱크대 누수 수리 기사 방문", "ko")]),
]


# 방해 음성 메모: 질의 없음. 실제 라이브러리처럼 음성이 여러 개일 때(전사 행 20개 이상)의 잘못된 결과를 본다.
DISTRACTORS = [
    ("ko-KR", "오늘 저녁은 된장찌개를 끓이고 냉장고에 남은 두부를 쓰기로 했어요."),
    ("ko-KR", "회사 앞 카페가 다음 달부터 리모델링 때문에 문을 닫는대요."),
    ("ko-KR", "아이 학교 운동회가 금요일이라 도시락을 싸야 해요."),
    ("ko-KR", "주말에 등산 갈 때 비 소식이 있으니 우비를 챙기자."),
    ("ko-KR", "세탁기 필터 청소를 한 달에 한 번은 해야 한다고 들었어요."),
    ("ko-KR", "도서관 책 반납 기한이 모레까지예요."),
    ("ko-KR", "강아지 예방 접종을 이번 주 안에 동물병원에서 맞혀야 해요."),
    ("ko-KR", "친구 결혼식 축의금 봉투를 미리 준비해 두세요."),
    ("ko-KR", "새로 산 노트북 화면에 밝은 점이 하나 보여서 교환을 문의했어요."),
    ("ko-KR", "베란다 화분에 물 주는 걸 잊지 마세요."),
    ("ko-KR", "부모님 댁 김장 날짜를 다음 주 토요일로 정했어요."),
    ("ko-KR", "지하철 정기권을 다음 달 것으로 충전해야 해요."),
    ("en-US", "The team meeting moved to three thirty because the client called."),
    ("en-US", "Buy oat milk, eggs, and a loaf of rye bread on the way home."),
    ("en-US", "My bike tire is flat again, I need a new inner tube."),
    ("en-US", "The library book about Roman history is due on Friday."),
    ("en-US", "Call the bank about the duplicate charge on my card."),
    ("en-US", "Water the tomatoes on the balcony every second day while I am away."),
    ("en-US", "The concert tickets will be sent by email a week before the show."),
    ("en-US", "Remind Sam to bring the projector cable to the workshop."),
]


# 받아쓰기와 측정


class _Counters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def peak_rss_mb(process: subprocess.Popen) -> float | None:
    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(wintypes.HANDLE(int(process._handle)), ctypes.byref(counters),
                                                  counters.cb)
    return round(counters.PeakWorkingSetSize / 2**20, 1) if ok else None


def gpu_used_mb() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def to_wav(source: Path, out: Path) -> float:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([media.find("ffmpeg"), "-v", "error", "-y", "-i", str(source), "-ar", "16000", "-ac", "1",
                    "-c:a", "pcm_s16le", str(out)], check=True)
    return media.probe(str(out))[0]


def transcribe(source: Path, name: str) -> dict:
    """whisper-cli 한 번(모델 로딩 포함). 언어는 자동 감지(제품에서 쓸 설정과 같게). 음성은 로컬에서만 처리한다."""
    wav = WORK / "wav" / f"{name}.wav"
    duration = to_wav(source, wav)
    out = WORK / "transcripts" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    baseline = gpu_used_mb()
    peak = {"gpu": baseline}
    done = threading.Event()

    def sample():
        while not done.is_set():
            used = gpu_used_mb()
            if used is not None and (peak["gpu"] is None or used > peak["gpu"]):
                peak["gpu"] = used
            done.wait(0.2)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    process = subprocess.Popen([str(WHISPER), "-m", str(MODEL), "-f", str(wav), "-l", "auto", "-oj", "-of", str(out)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _, err = process.communicate(timeout=3600)
    wall = time.perf_counter() - started
    rss = peak_rss_mb(process)
    done.set()
    sampler.join()
    err = err.decode("utf-8", "replace")
    if process.returncode != 0:
        raise RuntimeError(f"whisper-cli failed on {source.name}: {err[-400:]}")
    data = json.loads((out.with_suffix(".json")).read_text(encoding="utf-8"))
    load = re.search(r"load time =\s*([\d.]+) ms", err)
    total = re.search(r"total time =\s*([\d.]+) ms", err)
    segments = [(s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000, s["text"].strip()) for s in data["transcription"]]
    return {"file": name, "language": data["result"].get("language"), "duration_s": round(duration, 2),
            "wall_s": round(wall, 2), "load_ms": float(load.group(1)) if load else None,
            "total_ms": float(total.group(1)) if total else None,
            "realtime_factor": round(wall / duration, 3) if duration else None,
            "peak_rss_mb": rss, "gpu_baseline_mb": baseline,
            "gpu_peak_delta_mb": (peak["gpu"] - baseline) if baseline is not None and peak["gpu"] is not None else None,
            "text": " ".join(t for _, _, t in segments), "segments": segments}


def _norm(text: str, korean: bool) -> list[str]:
    text = search_text.normalized(text).lower()
    if korean:
        return [c for c in text if c.isalnum()]                    # 글자 단위 (띄어쓰기·문장부호 무시)
    return re.findall(r"[a-z0-9']+", text)


def error_rate(reference: str, hypothesis: str, korean: bool) -> float:
    """한국어는 CER(글자), 영어는 WER(단어). 숫자 표기 차이("열 시" ↔ "10시")도 그대로 오류로 센다."""
    ref, hyp = _norm(reference, korean), _norm(hypothesis, korean)
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i]
        for j, h in enumerate(hyp, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h)))
        previous = current
    return round(previous[-1] / max(1, len(ref)), 3)


# 실험 색인


def build_index(db: Path, roots: list[Path], embedder) -> dict:
    db.parent.mkdir(parents=True, exist_ok=True)
    with IndexStore(db) as store:
        report = Indexer(store, embedder).sync(roots)
    return {label(k): v for k, v in report.results.items() if not v.startswith("done")}   # 개인 경로를 남기지 않는다


def add_transcripts(source_db: Path, db: Path, transcripts: dict[str, dict], embedder, modality: str = "text") -> int:
    """음성 창(30초/25초 hop)마다 그 시간과 겹치는 받아쓰기 문장을 텍스트 구간 하나로 더한다(같은 loc, 키워드 + 문서 벡터)."""
    shutil.copyfile(source_db, db)
    added = 0
    with IndexStore(db) as store:
        for file_id, path in store.db.execute("SELECT id, path FROM files WHERE kind IN ('audio', 'video')").fetchall():
            found = transcripts.get(str(Path(path).resolve()).lower())
            if not found:
                continue
            windows = store.db.execute("SELECT loc, loc_end FROM segments WHERE file = ? AND kind = 'audio' ORDER BY loc",
                                       (file_id,)).fetchall()
            for start, end in windows or [(0.0, found["duration_s"])]:
                words = " ".join(t for a, b, t in found["segments"] if a < end and b > start).strip()
                if not words:
                    continue
                vector = embedder.embed_texts([prompts.document(words)])[0] if search_text.has_words(words) else None
                store.add_segment(file_id, "transcript", modality, loc=start, loc_end=end,
                                  excerpt=search_text.excerpt(words), text=words, vector=vector)
                added += 1
        store.commit()
    return added


def label(path: str) -> str:
    p = Path(path).resolve()
    for base, name in ((TEST_DATA, "test_data"), (HOLDOUT, "test_data_holdout"), (WORK / "independent", "independent"),
                       (WORK / "media_copy", "media_copy")):
        try:
            return f"{name}/{p.relative_to(base).as_posix()}"
        except ValueError:
            continue
    return p.name


def run(searcher: Searcher, vector, query: str, expect: str, span=None, limit: int = 5) -> dict:
    hits = searcher.search(query, vector, limit=limit)
    names = [label(h.path) for h in hits]
    rank = names.index(expect) + 1 if expect in names else None
    hit = hits[rank - 1] if rank else None
    place = None
    if hit and span is not None:
        place = hit.loc is not None and span[0] <= hit.loc <= span[1]
    z_rows = [h for h in hits if label(h.path) == expect]
    return {"rank": rank, "top": names[:3], "hits": len(hits), "above": names[:rank - 1] if rank else names,
            "segment": hit.segment if hit else None, "loc": hit.loc if hit else None, "place_ok": place,
            "z": round(z_rows[0].z, 2) if z_rows and z_rows[0].z is not None else None}


CONFIGS = ("base", "asr", "asr_own")


def compare(searchers: dict[str, Searcher], embedder, cases: list[tuple]) -> list[dict]:
    """cases: (질의, 기대 파일, 구간 또는 None, 태그). searchers: 설정 이름 → 검색기."""
    vectors = embedder.embed_texts([prompts.query(q) for q, *_ in cases])
    rows = []
    for (query, expect, span, tag), vector in zip(cases, vectors):
        row = {"q": query, "expect": expect, "tag": tag}
        for name, searcher in searchers.items():
            row[name] = run(searcher, vector, query, expect, span)
        rows.append(row)
    return rows


def media_in_top(rows: list[dict], key: str) -> int:
    """문서·그림 질의의 상위 5위에 음성·영상이 들어온 수(잘못된 결과)."""
    return sum(any(n.endswith((".m4a", ".wav", ".mp4")) for n in r[key]["top"]) for r in rows)


def score(rows: list[dict], key: str) -> dict:
    n = len(rows)
    return {"n": n, "r1": sum(r[key]["rank"] == 1 for r in rows), "r5": sum(r[key]["rank"] is not None for r in rows),
            "missing": sum(r[key]["rank"] is None for r in rows),
            "place_ok": sum(bool(r[key]["place_ok"]) for r in rows if r[key]["place_ok"] is not None)}


def evals_cases(path: Path, base_label: str) -> list[tuple]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [(q["q"], f"{base_label}/{q['expect']}", None, q["tag"]) for q in data["queries"]]


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "independent").mkdir(parents=True)
    # 1. 독립 합성 음성 (Windows 내장 TTS, test_data와 같은 방법)
    for name, culture, text, _ in INDEPENDENT:
        wav = WORK / "tts" / f"{Path(name).stem}.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        make_test_data.tts_wav(text, culture, wav)
        subprocess.run([media.find("ffmpeg"), "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "48k",
                        str(WORK / "independent" / name)], check=True)
    # 2. 받아쓰기 (각 파일마다 whisper-cli를 새로 띄움: 로딩 포함 시간)
    sources = [(TEST_DATA / "media" / "음성메모_0917.m4a", MEMO_TEXT, True),
               (TEST_DATA / "media" / "home_show_ep12.m4a", PODCAST_TEXT, False),
               (TEST_DATA / "media" / "clip_2026-08-30.mp4", NARRATION, False),
               (TEST_DATA / "media" / "room_tone.wav", "", False)]
    sources += [(WORK / "independent" / name, text, culture == "ko-KR") for name, culture, text, _ in INDEPENDENT]
    for n, (culture, text) in enumerate(DISTRACTORS):
        name = f"distractor_{n:02d}.m4a"
        wav = WORK / "tts" / f"distractor_{n:02d}.wav"
        make_test_data.tts_wav(text, culture, wav)
        subprocess.run([media.find("ffmpeg"), "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "48k",
                        str(WORK / "independent" / name)], check=True)
        sources.append((WORK / "independent" / name, text, culture == "ko-KR"))
    transcripts, accuracy = {}, []
    for source, reference, korean in sources:
        found = transcribe(source, source.stem)
        transcripts[str(source.resolve()).lower()] = found
        row = {k: v for k, v in found.items() if k != "segments"}
        row["reference"] = reference
        row["error_rate"] = error_rate(reference, found["text"], korean) if reference else None
        row["metric"] = "CER" if korean else "WER"
        if source.name == "음성메모_0917.m4a":
            row["keywords"] = {k: k in found["text"].replace(" ", "") for k in MEMO_KEYWORDS}
        accuracy.append(row)
        print(json.dumps({k: row[k] for k in ("file", "language", "text", "error_rate", "wall_s")}, ensure_ascii=False),
              flush=True)
    # 긴 녹음 속도: 팟캐스트를 반복해 약 10분 (합성 반복이라 정확도 판단에는 쓰지 않음)
    long_source = WORK / "long_podcast_10min.wav"
    subprocess.run([media.find("ffmpeg"), "-v", "error", "-y", "-stream_loop", "8", "-i",
                    str(TEST_DATA / "media" / "home_show_ep12.m4a"), "-t", "600", "-ar", "16000", "-ac", "1",
                    str(long_source)], check=True)
    long_run = {k: v for k, v in transcribe(long_source, "long_podcast_10min").items() if k not in ("segments", "text")}
    print(json.dumps(long_run), flush=True)

    # 3. 실험 색인 (.tmp만): A = test_data + 독립 음성, B = holdout + test_data/media 사본 + 독립 음성
    media_copy = WORK / "media_copy"
    shutil.copytree(TEST_DATA / "media", media_copy)
    with engine.Embedder() as embedder:
        started = time.perf_counter()
        not_done_a = build_index(WORK / "a_base.sqlite", [TEST_DATA, WORK / "independent"], embedder)
        not_done_b = build_index(WORK / "b_base.sqlite", [HOLDOUT, media_copy, WORK / "independent"], embedder)
        index_seconds = time.perf_counter() - started
        for src in sorted(media_copy.iterdir()):   # 사본도 같은 전사를 쓴다(같은 파일 내용)
            original = transcripts.get(str((TEST_DATA / "media" / src.name).resolve()).lower())
            if original:
                transcripts[str(src.resolve()).lower()] = original
        added_a = add_transcripts(WORK / "a_base.sqlite", WORK / "a_asr.sqlite", transcripts, embedder)
        added_b = add_transcripts(WORK / "b_base.sqlite", WORK / "b_asr.sqlite", transcripts, embedder)
        add_transcripts(WORK / "a_base.sqlite", WORK / "a_asr_own.sqlite", transcripts, embedder, "transcript")
        add_transcripts(WORK / "b_base.sqlite", WORK / "b_asr_own.sqlite", transcripts, embedder, "transcript")

        stores = {f"{side}_{c}": IndexStore(WORK / f"{side}_{c}.sqlite") for side in "ab" for c in CONFIGS}
        try:
            a = {c: Searcher(stores[f"a_{c}"]) for c in CONFIGS}
            b = {c: Searcher(stores[f"b_{c}"]) for c in CONFIGS}
            u1 = compare(a, embedder, [(q, MEMO, None, f"u1-{lang}") for q, lang in U1_QUERIES])
            independent = compare(a, embedder, [(q, f"independent/{name}", None, f"indep-{lang}")
                                                for name, _, _, queries in INDEPENDENT for q, lang in queries])
            media_rows = compare(a, embedder, [(q, e, span, tag) for q, e, span, tag in MEDIA_QUERIES])
            documents = compare(a, embedder, evals_cases(ROOT / "evals" / "test_data.json", "test_data"))
            import test_index_search as stage2   # 2단계 질의 22개 (문서·그림·PDF)
            stage2_rows = compare(a, embedder, [(q, f"test_data/{e}", None, "stage2") for q, e, _ in stage2.QUERIES])
            holdout = compare(b, embedder, evals_cases(ROOT / "evals" / "holdout.json", "test_data_holdout"))
            independent_b = compare(b, embedder, [(q, f"independent/{name}", None, f"indep-{lang}")
                                                  for name, _, _, queries in INDEPENDENT for q, lang in queries])
        finally:
            for s in stores.values():
                s.close()

    groups = {"u1": u1, "independent_audio": independent, "independent_audio_in_holdout_index": independent_b,
              "media_regression": media_rows, "documents_eval": documents, "stage2": stage2_rows,
              "holdout_eval": holdout}
    summary = {name: {c: score(rows, c) for c in CONFIGS} for name, rows in groups.items()}
    for name in ("documents_eval", "stage2", "holdout_eval"):
        summary[name]["media_in_top5"] = {c: media_in_top(groups[name], c) for c in CONFIGS}
    for name in ("u1", "independent_audio"):
        for lang in ("en", "ko"):
            rows = [r for r in groups[name] if r["tag"].endswith(lang)]
            summary[name][lang] = {f"{c}_r1": sum(r[c]["rank"] == 1 for r in rows) for c in CONFIGS}
            summary[name][lang]["n"] = len(rows)
    out = {"measured": time.strftime("%Y-%m-%d %H:%M"),
           "setup": {"whisper": "whisper.cpp b5454 whisper-bin-win-cuda-12.4.0-x64", "model": MODEL.name,
                     "language": "auto", "decoding": "whisper-cli defaults (beam 5, best-of 5, flash attention)",
                     "transcript_segment": "30 s audio window, same loc; keywords + document vector (title none)",
                     "configs": {"base": "audio vectors only (current product)",
                                 "asr": "+ transcript segments, modality text",
                                 "asr_own": "+ transcript segments, own modality 'transcript' (per-modality mean)"},
                     "distractor_memos": len(DISTRACTORS),
                     "search": "product Searcher unchanged (WORD_WEIGHT 2.0, z gate 2.0, setsScale ON)"},
           "index_seconds_both_bases": round(index_seconds, 1), "not_done": {"a": not_done_a, "b": not_done_b},
           "transcript_segments_added": {"a": added_a, "b": added_b},
           "accuracy": accuracy, "long_audio_speed": long_run, "summary": summary, "rows": groups}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "u1-asr.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
