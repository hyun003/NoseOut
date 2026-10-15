"""U1 2차 실험: 전사 검색의 부작용(무관한 음성이 끼어듦)을 줄이는 후보를 고르고, 따로 둔 데이터로 최종 확인한다.
제품 코드에는 연결하지 않는다. 추가 설치 없음(whisper.cpp는 1차 실험 때 설치).

비교 기준 (1차와 같음): base(음성 벡터만, 지금 제품), text(30초 창 전사를 text 모달로), own(30초 창 전사를 자기 모달로).
새 후보 = "전사 채널" (기존 점수를 바꾸지 않음):
  - 문서·이미지·음성 결과의 점수는 전사 없는 색인에서 제품 Searcher가 낸 값 그대로다(setsScale ON, 점수식 그대로).
  - 전사만 따로 점수를 매긴다: z_t = (전사 코사인 − 배경 평균 − c̄) / 표준편차. 표준편차·c̄는 제품 meaning()이 그 질의에서
    쓴 값, 배경 평균은 질의와 고정된 짧은 일반 문장 60개(BACKGROUND)의 평균 코사인. 짧은 글은 어떤 질의와도 코사인이 높다는
    1차 측정(0.590 vs 0.556)을 라이브러리 크기와 상관없이 상쇄한다(전사가 1개여도 같다).
  - 전사 점수 = 제품과 같은 가중치(뜻 1·단어 2, 번호 검색은 0.5·4)·종류 힌트. 후보 조건은 z_t ≥ 전사 기준(gate) 또는 전사
    키워드 절반 이상. 파일 점수 = max(기존 점수, 전사 점수): 전사는 파일을 올릴 수만 있고 다른 파일 점수는 바꾸지 않는다.
  - 변형: 묶음(30초 창 / 파일 단위), gate 2.0·2.5·3.0·3.5, 전사 키워드 켬/끔, 제목(없음 / 파일 이름).

데이터 분리: 선택(dev)과 최종 확인(test)을 나눈다. 선택 규칙(select)은 test를 보기 전에 정해 두었다.
  dev: 1차 독립 메모 6개(질의 13) + 방해 20개, test_data 문서 평가 29·2단계 22, 기존 음성·영상 8.
  test: 새 메모 6개(파일 이름은 rec_01… — 이름 단서 없음, 질의 12개는 받아쓰기 전에 고정) + 새 방해 20개, U1 8개, holdout 32.
  각각 큰 라이브러리(전사 20개 이상)와 작은 라이브러리(방해 메모 없음, 전사 20개 미만)를 둔다.
받아쓰기 결과는 손대지 않는다. 결과: results/u1-asr2.json, 실험 파일은 .tmp/asr-eval2에만.

    .venv\\Scripts\\python scripts\\eval_asr2.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import PySide6.QtCore  # noqa: E402,F401  (winrt보다 먼저)

import eval_asr  # noqa: E402  (1차 실험의 받아쓰기·색인 도구, 질의, 메모)
import make_test_data  # noqa: E402
from noseout import engine, media, prompts, search_text  # noqa: E402
from noseout import searcher as searcher_module  # noqa: E402
from noseout.searcher import Searcher  # noqa: E402
from noseout.store import TOKENIZER, IndexStore  # noqa: E402

WORK = ROOT / ".tmp" / "asr-eval2"
eval_asr.WORK = WORK            # 1차 도구가 wav·전사를 이 폴더에 쓰게
TEST_DATA, HOLDOUT = ROOT / "test_data", ROOT / "test_data_holdout"

# 최종 확인용 새 메모: 이름 단서가 없는 파일 이름, 질의는 받아쓰기 전에 고정
TEST_MEMOS = [
    ("rec_01.m4a", "ko-KR", "세탁소에 맡긴 겨울 코트를 목요일 저녁에 찾아와야 해요.",
     [("pick up the winter coat from the dry cleaner", "en"), ("세탁소 코트 찾기", "ko")]),
    ("rec_02.m4a", "ko-KR", "운전면허증 갱신 기간이 이번 달 말까지라서 사진을 새로 찍어야 해요.",
     [("renew the driver's license", "en"), ("운전면허 갱신 사진", "ko")]),
    ("rec_03.m4a", "ko-KR", "우리 집 고양이 사료가 떨어져서 온라인으로 두 포대를 주문했어요.",
     [("ordered cat food online", "en"), ("고양이 사료 주문", "ko")]),
    ("rec_04.m4a", "ko-KR", "치킨 배달이 한 시간 넘게 안 와서 가게에 전화했어요.",
     [("the food delivery was late", "en"), ("배달 늦어서 전화", "ko")]),
    ("rec_05.m4a", "en-US", "Pay the gas bill before the twentieth or there will be a late fee.",
     [("gas bill due date", "en"), ("가스 요금 납부 기한", "ko")]),
    ("rec_06.m4a", "en-US", "Our flight to Jeju leaves at seven, so we should be at the airport by five thirty.",
     [("what time to get to the airport", "en"), ("제주도 비행기 시간", "ko")]),
]
TEST_DISTRACTORS = [
    ("ko-KR", "점심으로 김밥 두 줄이랑 라면을 먹었어요."), ("ko-KR", "이번 주말에 대청소를 하면서 옷장 정리도 할 거예요."),
    ("ko-KR", "동생이 다음 달에 이사를 가서 짐 싸는 걸 도와주기로 했어요."), ("ko-KR", "요가 수업이 화요일과 목요일 저녁 일곱 시에 있어요."),
    ("ko-KR", "아침마다 공원에서 삼십 분씩 걷기로 했어요."), ("ko-KR", "새 커튼을 달았더니 방이 훨씬 밝아졌어요."),
    ("ko-KR", "프린터 잉크가 떨어져서 문서를 출력하지 못했어요."), ("ko-KR", "냉장고 정리를 하다가 유통기한 지난 우유를 버렸어요."),
    ("ko-KR", "버스 노선이 바뀌어서 출근 시간이 십 분 늘었어요."), ("ko-KR", "할머니 댁에 가져갈 과일 바구니를 샀어요."),
    ("ko-KR", "주차장에 세워 둔 차 앞유리에 낙엽이 잔뜩 쌓였어요."), ("ko-KR", "베란다에 둔 자전거 바퀴에 바람이 빠졌어요."),
    ("en-US", "I left my umbrella at the coffee shop near the station."),
    ("en-US", "The new keyboard arrived but the space bar feels a bit sticky."),
    ("en-US", "Let's try the Thai place on Fifth Avenue for dinner tonight."),
    ("en-US", "I finally fixed the squeaky door hinge with a bit of oil."),
    ("en-US", "The kids want to build a treehouse this summer."),
    ("en-US", "Remember to back up the laptop before the software update."),
    ("en-US", "The neighbor's dog keeps barking every night around midnight."),
    ("en-US", "We are out of dish soap and paper towels."),
]
# 배경: 평가 메모·질의와 관계없는 짧은 일반 문장. 제품에 넣는다면 고정 자료로 함께 배포하는 셈이다.
BACKGROUND = [
    "오늘은 하늘이 맑고 바람이 조금 불어요.", "잠깐 쉬었다가 다시 시작할게요.", "그 얘기는 나중에 다시 하자.",
    "지금 막 집에 도착했어요.", "다들 잘 지내고 있지요?", "생각보다 시간이 오래 걸렸네요.", "조금만 기다려 주세요.",
    "이건 어디에 두면 될까요?", "다음에 또 연락할게요.", "오늘 하루도 수고 많으셨어요.", "방금 메시지를 확인했어요.",
    "그건 잘 모르겠어요.", "이따가 다시 전화할게요.", "벌써 저녁이 다 됐네요.", "날씨가 점점 추워지고 있어요.",
    "어제 이야기한 대로 진행할게요.", "그럼 그렇게 하기로 해요.", "오랜만에 산책을 했어요.", "음악을 틀어 놓고 일했어요.",
    "생각난 김에 적어 둘게요.", "창문을 조금 열어 두었어요.", "길이 생각보다 막혔어요.", "이번 주는 조금 바빴어요.",
    "물 한 잔 마시고 올게요.", "곧 출발할 거예요.", "이 부분은 다시 확인해 볼게요.", "별일 없이 잘 끝났어요.",
    "내일 다시 이야기해요.", "오후에는 좀 한가할 것 같아요.", "괜찮으면 같이 가요.",
    "It is a little windy outside today.", "Let me take a short break and come back.", "We can talk about that later.",
    "I just got home.", "I hope everyone is doing well.", "That took longer than I expected.", "Please wait a moment.",
    "Where should I put this?", "I will get in touch again soon.", "Thanks for all your work today.",
    "I just saw your message.", "I am not sure about that.", "I will call you back in a bit.",
    "It is already getting dark.", "The weather is slowly getting colder.", "Let's go ahead as we discussed.",
    "Okay, let's do it that way.", "I went for a walk for the first time in a while.", "I worked with some music on.",
    "Let me write this down before I forget.", "I left the window open a little.", "Traffic was worse than I thought.",
    "This week has been a bit busy.", "I will grab a glass of water.", "I am leaving soon.", "I will double check this part.",
    "Everything went fine.", "Let's talk again tomorrow.", "The afternoon should be quieter.", "Come along if you like.",
]
GATES = (2.0, 2.5, 3.0, 3.5)


def make_memos(folder: Path, memos: list[tuple], distractors: list[tuple], prefix: str) -> list[tuple]:
    """TTS로 메모를 만들고 (경로, 원문, 한국어인지) 목록을 돌려준다. distractors는 folder/distractors에."""
    out = []
    items = [(folder / name, culture, text) for name, culture, text, *_ in memos]
    items += [(folder / "distractors" / f"{prefix}_{n:02d}.m4a", c, t) for n, (c, t) in enumerate(distractors)]
    for path, culture, text in items:
        path.parent.mkdir(parents=True, exist_ok=True)
        wav = WORK / "tts" / f"{path.parent.name}_{path.stem}.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        make_test_data.tts_wav(text, culture, wav)
        subprocess.run([media.find("ffmpeg"), "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "48k",
                        str(path)], check=True)
        out.append((path, text, culture == "ko-KR"))
    return out


LABELS: list[tuple[Path, str]] = []


def label(path: str) -> str:
    p = Path(path).resolve()
    for base, name in LABELS:
        try:
            return f"{name}/{p.relative_to(base).as_posix()}"
        except ValueError:
            continue
    return p.name


def is_media(name: str) -> bool:
    return name.endswith((".m4a", ".wav", ".mp4"))


# 전사 채널 (실험용, 제품 코드 아님)


class TranscriptChannel:
    """한 실험 색인의 전사 구간들. 기존 결과는 base Searcher 그대로 두고 전사만 따로 점수를 매겨 합친다."""

    def __init__(self, store: IndexStore, transcripts: dict, embedder, granularity: str, titled: bool):
        self.rows = []   # (file id, path, kind, loc, text)
        for file_id, path, kind in store.db.execute("SELECT id, path, kind FROM files WHERE kind IN ('audio','video')"):
            found = transcripts.get(str(Path(path).resolve()).lower())
            if not found:
                continue
            if granularity == "file":
                spans = [(0.0, found["duration_s"])]
            else:
                spans = store.db.execute("SELECT loc, loc_end FROM segments WHERE file = ? AND kind = 'audio' ORDER BY loc",
                                         (file_id,)).fetchall() or [(0.0, found["duration_s"])]
            for start, end in spans:
                words = " ".join(t for a, b, t in found["segments"] if a < end and b > start).strip()
                if words and search_text.has_words(words):
                    self.rows.append((file_id, path, kind, start, words))
        texts = [prompts.document(text, Path(path).stem if titled else None) for _, path, _, _, text in self.rows]
        self.matrix = np.asarray(embedder.embed_texts(texts), np.float32) if texts else np.zeros((0, 768), np.float32)
        self.fts = sqlite3.connect(":memory:")
        self.fts.execute(f'CREATE VIRTUAL TABLE t USING fts5(body, tokenize = "{TOKENIZER}")')
        self.fts.executemany("INSERT INTO t(rowid, body) VALUES(?, ?)",
                             [(i, search_text.normalized(r[4])) for i, r in enumerate(self.rows)])

    def search(self, base: Searcher, query: str, vector, background: np.ndarray, gate: float, keywords: bool,
               limit: int = 5) -> list[tuple[str, float, str, float | None]]:
        """[(경로, 점수, 가리킨 구간 종류, loc)] 상위 limit개."""
        hits = base.search(query, vector, limit=200)
        merged = {h.path: (h.score, h.segment, h.loc) for h in hits}
        if len(self.rows):
            v = np.asarray(vector, np.float32)
            scores = base._scores(v)
            scale, modality = base.row_sets_scale, base.row_modality
            overall = float(scores[scale].mean())
            centered = scores.astype(np.float64).copy()
            for m in np.unique(modality):
                mask = modality == m
                counted = mask & scale
                centered[mask] -= scores[counted].mean() if counted.sum() >= 20 else overall
            deviation = max(float(centered[scale].std()), 0.02)
            offset = float(centered[scale].mean())
            z = (self.matrix @ v - float((background @ v).mean()) - offset) / deviation
            best: dict[int, tuple[float, int]] = {}
            for i, value in enumerate(z):
                file = self.rows[i][0]
                if file not in best or value > best[file][0]:
                    best[file] = (float(value), i)
            words = searcher_module.terms(query)
            pats = searcher_module.patterns(words)
            coverage: dict[int, float] = {}
            if keywords and pats:
                for pattern in pats:
                    files = {self.rows[r][0] for (r,) in self.fts.execute("SELECT rowid FROM t WHERE t MATCH ?",
                                                                          (pattern,))}
                    for file in files:
                        coverage[file] = coverage.get(file, 0) + 1 / len(pats)
            lookup = searcher_module.is_lookup(words)
            mw, ww = (0.5, searcher_module.WORD_WEIGHT * 2) if lookup else (1.0, searcher_module.WORD_WEIGHT)
            hinted = searcher_module.kind_hints(query)
            for file, (value, i) in best.items():
                cov = coverage.get(file, 0)
                if value < gate and cov < 0.5:
                    continue
                path, kind = self.rows[i][1], self.rows[i][2]
                score = mw * max(value, 0) + ww * cov ** 1.5 + (1 if kind in hinted else 0)
                if score > merged.get(path, (-1e9,))[0]:
                    merged[path] = (score, "transcript", self.rows[i][3])
        ranked = sorted(merged.items(), key=lambda kv: -kv[1][0])[:limit]
        return [(path, score, seg, loc) for path, (score, seg, loc) in ranked]


def outcome(results: list[tuple], expect: str, span) -> dict:
    names = [label(p) for p, *_ in results]
    rank = names.index(expect) + 1 if expect in names else None
    place = None
    if rank and span is not None:
        loc = results[rank - 1][3]
        place = loc is not None and span[0] <= loc <= span[1]
    return {"rank": rank, "top": names[:5], "place_ok": place,
            "segment": results[rank - 1][2] if rank else None}


def product_results(searcher: Searcher, query: str, vector) -> list[tuple]:
    return [(h.path, h.score, h.segment, h.loc) for h in searcher.search(query, vector, limit=5)]


# 지표


def audio_metrics(rows: list[dict], config: str) -> dict:
    """음성 질의: 1위, 상위 5위, 누락, 정답보다 위에 온 다른 음성 수."""
    out = {"n": len(rows), "r1": 0, "r5": 0, "missing": 0, "wrong_audio_above": 0, "place_ok": 0, "place_n": 0}
    for r in rows:
        o = r[config]
        out["r1"] += o["rank"] == 1
        out["r5"] += o["rank"] is not None
        out["missing"] += o["rank"] is None
        above = o["top"][:o["rank"] - 1] if o["rank"] else o["top"]
        out["wrong_audio_above"] += sum(is_media(n) for n in above)
        if o["place_ok"] is not None:
            out["place_n"] += 1
            out["place_ok"] += bool(o["place_ok"])
    return out


def doc_metrics(rows: list[dict], config: str) -> dict:
    """문서·그림 질의: 1위, 기존(base)에서 찾던 결과의 누락·1위 상실, 끼어든 음성·영상(상위 5위·1위)."""
    out = {"n": len(rows), "r1": 0, "r5": 0, "lost_r1": 0, "lost_r5": 0, "media_in_top5": 0, "media_at_top1": 0,
           "queries_with_media_in_top5": 0}
    for r in rows:
        o, b = r[config], r["base"]
        out["r1"] += o["rank"] == 1
        out["r5"] += o["rank"] is not None
        out["lost_r1"] += b["rank"] == 1 and o["rank"] != 1
        out["lost_r5"] += b["rank"] is not None and o["rank"] is None
        media_names = [n for n in o["top"] if is_media(n)]
        out["media_in_top5"] += len(media_names)
        out["queries_with_media_in_top5"] += bool(media_names)
        out["media_at_top1"] += bool(o["top"]) and is_media(o["top"][0])
    return out


def evaluate(scenario: dict, configs: dict, embedder, background) -> dict:
    """scenario: {"audio": [(q, expect, span, tag)], "docs": [...]}; configs: 이름 → (종류, 인자)."""
    result = {}
    for group in ("audio", "docs"):
        cases = scenario[group]
        vectors = embedder.embed_texts([prompts.query(q) for q, *_ in cases])
        rows = []
        for (query, expect, span, tag), vector in zip(cases, vectors):
            row = {"q": query, "expect": expect, "tag": tag}
            for name, (kind, args) in configs.items():
                if kind == "product":
                    found = product_results(args, query, vector)
                else:
                    channel, base, gate, keywords = args
                    found = channel.search(base, query, vector, background, gate, keywords)
                row[name] = outcome(found, expect, span)
            rows.append(row)
        result[group] = rows
    return result


def summarize(result: dict, configs: list[str], tag_groups: dict[str, str]) -> dict:
    out = {}
    for name in configs:
        entry = {}
        for group_name, prefix in tag_groups.items():
            rows = [r for r in result["audio"] if r["tag"].startswith(prefix)]
            if rows:
                entry[group_name] = audio_metrics(rows, name)
        entry["docs"] = doc_metrics(result["docs"], name)
        out[name] = entry
    return out


def eligible(summary: dict, name: str) -> bool:
    """선택 규칙 (test를 보기 전에 정함): 문서 질의의 1위·상위 5위를 하나도 잃지 않고, 끼어든 음성이 base보다 늘지 않으며,
    기존 음성·영상 질의의 1위와 시점이 base보다 나빠지지 않을 것."""
    s, b = summary[name], summary["base"]
    return (s["docs"]["lost_r1"] == 0 and s["docs"]["lost_r5"] == 0
            and s["docs"]["media_in_top5"] <= b["docs"]["media_in_top5"]
            and s["media"]["r1"] >= b["media"]["r1"] and s["media"]["place_ok"] >= b["media"]["place_ok"])


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    dev_folder, test_folder = WORK / "dev_memos", WORK / "test_memos"
    media_copy = WORK / "media_copy"
    shutil.copytree(TEST_DATA / "media", media_copy)
    LABELS.extend([(TEST_DATA, "test_data"), (HOLDOUT, "test_data_holdout"), (dev_folder, "dev"), (test_folder, "test"),
                   (media_copy, "media_copy")])
    dev_sources = make_memos(dev_folder, eval_asr.INDEPENDENT, eval_asr.DISTRACTORS, "dev")
    test_sources = make_memos(test_folder, TEST_MEMOS, TEST_DISTRACTORS, "test")
    # 받아쓰기 (손대지 않음)
    transcripts, accuracy = {}, []
    sources = [(TEST_DATA / "media" / n, ref, ko) for n, ref, ko in (
        ("음성메모_0917.m4a", eval_asr.MEMO_TEXT, True), ("home_show_ep12.m4a", eval_asr.PODCAST_TEXT, False),
        ("clip_2026-08-30.mp4", eval_asr.NARRATION, False))] + dev_sources + test_sources
    for source, reference, korean in sources:
        found = eval_asr.transcribe(source, f"{source.parent.name}_{source.stem}")
        transcripts[str(source.resolve()).lower()] = found
        accuracy.append({"file": label(str(source)), "language": found["language"], "text": found["text"],
                         "reference": reference, "metric": "CER" if korean else "WER",
                         "error_rate": eval_asr.error_rate(reference, found["text"], korean), "wall_s": found["wall_s"]})
    for src in media_copy.iterdir():
        original = transcripts.get(str((TEST_DATA / "media" / src.name).resolve()).lower())
        if original:
            transcripts[str(src.resolve()).lower()] = original

    # 실험 색인 4벌 (.tmp만). small = 방해 메모 없음(전사 20개 미만)
    roots = {
        "dev_large": [TEST_DATA, dev_folder], "dev_small": [TEST_DATA, dev_folder],
        "test_large": [HOLDOUT, media_copy, test_folder], "test_small": [HOLDOUT, media_copy, test_folder],
    }
    exclude_distractors = {"dev_small", "test_small"}
    report = {"measured": time.strftime("%Y-%m-%d %H:%M"), "accuracy": accuracy, "scenarios": {}}
    with engine.Embedder() as embedder:
        background = np.asarray(embedder.embed_texts([prompts.document(t) for t in BACKGROUND]), np.float32)
        built = {}
        for name, folders in roots.items():
            db = WORK / f"{name}_base.sqlite"
            with IndexStore(db) as store:
                from noseout.indexer import Indexer
                exclusions = [folders[-1] / "distractors"] if name in exclude_distractors else None
                Indexer(store, embedder).sync(folders, exclusions=exclusions)
            eval_asr.add_transcripts(db, WORK / f"{name}_text.sqlite", transcripts, embedder, "text")
            eval_asr.add_transcripts(db, WORK / f"{name}_own.sqlite", transcripts, embedder, "transcript")
            built[name] = db

        dev_audio = ([(q, f"dev/{n}", None, f"indep-{lang}") for n, _, _, qs in eval_asr.INDEPENDENT for q, lang in qs]
                     + [(q, e, span, f"media-{tag}") for q, e, span, tag in eval_asr.MEDIA_QUERIES])
        test_audio = ([(q, f"test/{n}", None, f"indep-{lang}") for n, _, _, qs in TEST_MEMOS for q, lang in qs]
                      + [(q, "media_copy/음성메모_0917.m4a", None, f"u1-{lang}") for q, lang in eval_asr.U1_QUERIES]
                      + [(q, e.replace("test_data/media/", "media_copy/"), span, f"media-{tag}")
                         for q, e, span, tag in eval_asr.MEDIA_QUERIES])
        import test_index_search as stage2
        dev_docs = (eval_asr.evals_cases(ROOT / "evals" / "test_data.json", "test_data")
                    + [(q, f"test_data/{e}", None, "stage2") for q, e, _ in stage2.QUERIES])
        test_docs = eval_asr.evals_cases(ROOT / "evals" / "holdout.json", "test_data_holdout")
        scenarios = {"dev_large": (dev_audio, dev_docs), "dev_small": (dev_audio, dev_docs),
                     "test_large": (test_audio, test_docs), "test_small": (test_audio, test_docs)}
        tag_groups = {"indep": "indep", "u1": "u1", "media": "media"}

        candidate_names = []
        results = {}
        for name, (audio, docs) in scenarios.items():
            stores = {k: IndexStore(WORK / f"{name}_{k}.sqlite") for k in ("base", "text", "own")}
            try:
                base = Searcher(stores["base"])
                configs = {"base": ("product", base), "text": ("product", Searcher(stores["text"])),
                           "own": ("product", Searcher(stores["own"]))}
                channels = {}
                for granularity in ("window", "file"):
                    for titled in (False, True):
                        channels[(granularity, titled)] = TranscriptChannel(stores["base"], transcripts, embedder,
                                                                            granularity, titled)
                for (granularity, titled), channel in channels.items():
                    for gate in GATES:
                        for keywords in (True, False):
                            if titled and not keywords:
                                continue
                            cname = f"ch-{granularity}{'-title' if titled else ''}-g{gate}{'' if keywords else '-nokw'}"
                            configs[cname] = ("channel", (channel, base, gate, keywords))
                if not candidate_names:
                    candidate_names = [c for c in configs if c.startswith("ch-")]
                results[name] = evaluate({"audio": audio, "docs": docs}, configs, embedder, background)
                report["scenarios"][name] = {
                    "transcript_rows_window": len(channels[("window", False)].rows),
                    "transcript_rows_file": len(channels[("file", False)].rows),
                    "summary": summarize(results[name], list(configs), tag_groups)}
            finally:
                for s in stores.values():
                    s.close()

    # 선택: dev 두 경우 모두 규칙을 지키는 후보 가운데 dev 독립 음성 1위 합이 가장 큰 것, 같으면 gate가 높은(보수적) 것
    dev = report["scenarios"]
    passing = [c for c in candidate_names if eligible(dev["dev_large"]["summary"], c)
               and eligible(dev["dev_small"]["summary"], c)]
    gain = {c: dev["dev_large"]["summary"][c]["indep"]["r1"] + dev["dev_small"]["summary"][c]["indep"]["r1"] for c in passing}
    chosen = max(passing, key=lambda c: (gain[c], float(c.split("-g")[1].split("-")[0]))) if passing else None
    report["selection"] = {"rule": eligible.__doc__.strip(), "passing_on_dev": passing, "dev_indep_r1_sum": gain,
                           "chosen": chosen,
                           "base_dev_indep_r1_sum": dev["dev_large"]["summary"]["base"]["indep"]["r1"]
                           + dev["dev_small"]["summary"]["base"]["indep"]["r1"]}
    report["setup"] = {"background_sentences": len(BACKGROUND), "gates": GATES,
                       "channel": "base scores unchanged; transcript z = (cos - mean background cos - c) / product "
                                  "deviation; file score = max(base, transcript)",
                       "test_memo_names": "rec_01..06 (no name hints)"}
    report["rows"] = {k: v for k, v in results.items()}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "u1-asr2.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["selection"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
