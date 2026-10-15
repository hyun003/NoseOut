"""같은 뜻의 텍스트·이미지·음성이 가까운 벡터가 되는지 확인한다 (docs/model-validation.md의 방법).

각 모달마다 N개의 입력과 N개의 설명(검색어)을 짝지어 N×N 코사인 행렬을 만들고 다음을 본다.
- top-1 정확도: 검색어마다 짝이 1등인지 (텍스트→미디어, 미디어→텍스트 둘 다)
- margin: 짝의 코사인 − 같은 행에서 가장 높은 비짝 코사인
- 모달 간격: 텍스트↔텍스트와 텍스트↔이미지 코사인의 크기 차이 (원본 Searcher가 모달별 평균을 빼는 이유)
"""

import json

import numpy as np
import pytest

from conftest import FIXTURES, REFERENCE, requires_engine
from noseout import prompts

pytestmark = requires_engine

IMAGE_CAPTIONS = {
    "red_circle": ("a red circle on a white background", "흰 배경 위의 빨간 원"),
    "bar_chart": ("a blue bar chart", "파란 막대 그래프"),
    "stop_sign": ("a red stop sign", "빨간 정지 표지판"),
    "invoice": ("an invoice with a total amount due", "합계 금액이 적힌 청구서"),
    "night_sky": ("a night sky with stars and a crescent moon", "별과 초승달이 있는 밤하늘"),
    "@photo": ("a landscape with green hills, a tree and the sun", "초록 언덕과 나무, 해가 있는 풍경"),
    "@screen": ("a screenshot of an app window with an error message", "오류 메시지가 뜬 앱 화면 캡처"),
}

SPEECH_QUERIES = {
    "meeting_en": ("when is the team meeting", "팀 회의 일정 변경"),
    "weather_en": ("rain forecast, take an umbrella", "내일 비 예보"),
    "recipe_en": ("how to cook pasta with tomato sauce", "토마토 파스타 만드는 법"),
    "train_ko": ("train from Seoul to Busan departure time", "서울에서 부산 가는 기차 시간"),
}

TEXT_PAIRS = [
    ("payment declined error", "Your card was declined. Please try another payment method."),
    ("주차 요금 정산 방법", "Parking fees can be paid at the machine near the exit or by app."),
    ("how to reset the router", "공유기 뒷면의 리셋 버튼을 10초간 누르면 공장 초기화됩니다."),
    ("vacation policy days off", "Employees receive fifteen paid leave days per year."),
    ("python list sort descending", "Use sorted(items, reverse=True) to order from largest to smallest."),
]


def image_path(name):
    return REFERENCE / {"@photo": "photo.jpg", "@screen": "screen.png"}[name] if name.startswith("@") \
        else FIXTURES / f"{name}.png"


def evaluate(queries: np.ndarray, items: np.ndarray) -> dict:
    sims = queries @ items.T
    n = len(sims)
    diag = np.diag(sims)
    off = sims[~np.eye(n, dtype=bool)].reshape(n, n - 1)
    q2i = float(np.mean(np.argmax(sims, axis=1) == np.arange(n)))
    i2q = float(np.mean(np.argmax(sims, axis=0) == np.arange(n)))
    return {
        "top1_query_to_item": round(q2i, 3),
        "top1_item_to_query": round(i2q, 3),
        "matched_mean": round(float(diag.mean()), 4),
        "unmatched_mean": round(float(off.mean()), 4),
        "min_margin": round(float(np.min(diag - off.max(axis=1))), 4),
        "matrix": np.round(sims, 3).tolist(),
    }


@pytest.fixture(scope="module")
def image_vectors(embedder):
    return np.stack([embedder.embed_image(str(image_path(n)), 280) for n in IMAGE_CAPTIONS])


@pytest.mark.parametrize("lang", [0, 1], ids=["en", "ko"])
def test_text_to_image(embedder, image_vectors, record, lang):
    queries = embedder.embed_texts([prompts.query(c[lang]) for c in IMAGE_CAPTIONS.values()])
    result = evaluate(queries, image_vectors)
    result["items"] = list(IMAGE_CAPTIONS)
    record.setdefault("text_to_image", {})[["en", "ko"][lang]] = result
    assert result["matched_mean"] > result["unmatched_mean"]
    assert result["top1_query_to_item"] >= 5 / 7
    # 반대 방향(이미지 → 설명)도 본다. 추상 스크린샷 1개가 틀리는 것이 알려진 결과(6/7, model-validation.md 4.2)
    assert result["top1_item_to_query"] >= 5 / 7


@pytest.mark.parametrize("lang", [0, 1], ids=["en", "ko"])
def test_text_to_speech(embedder, record, lang):
    meta = json.loads((FIXTURES / "speech.json").read_text(encoding="utf-8"))
    names = [n for n in SPEECH_QUERIES if n in meta]
    if len(names) < 2:
        pytest.skip("TTS fixtures missing: run scripts/make_fixtures.py")
    audio = np.stack([embedder.embed_audio(str(FIXTURES / f"{n}.wav")) for n in names])
    queries = embedder.embed_texts([prompts.query(SPEECH_QUERIES[n][lang]) for n in names])
    result = evaluate(queries, audio)
    result["items"] = names
    record.setdefault("text_to_speech", {})[["en", "ko"][lang]] = result
    assert result["matched_mean"] > result["unmatched_mean"]
    assert result["top1_query_to_item"] >= 0.75
    assert result["top1_item_to_query"] >= 0.75


def test_text_to_text_multilingual(embedder, record):
    queries = embedder.embed_texts([prompts.query(q) for q, _ in TEXT_PAIRS])
    docs = embedder.embed_texts([prompts.document(d) for _, d in TEXT_PAIRS])
    result = evaluate(queries, docs)
    record["text_to_text"] = result
    assert result["top1_query_to_item"] == 1.0


def test_modality_gap(record):
    """기록용: 같은 짝이라도 텍스트↔텍스트 코사인이 텍스트↔이미지보다 큰지. 크면 검색에서 모달별 정규화가 필요하다."""
    tt = record.get("text_to_text", {}).get("matched_mean")
    ti = record.get("text_to_image", {}).get("en", {}).get("matched_mean")
    ts = record.get("text_to_speech", {}).get("en", {}).get("matched_mean")
    if tt is None or ti is None:
        pytest.skip("needs the other cross-modal tests in the same run")
    record["modality_gap"] = {"text_text_matched": tt, "text_image_matched": ti, "text_speech_matched": ts}
