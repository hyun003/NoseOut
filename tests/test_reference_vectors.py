"""원본 DigUp의 ReferenceVectorTests와 같은 입력으로, 이 엔진이 원본(macOS, llama.cpp b11461 Metal)과 같은 벡터를
만드는지 확인한다. 기준: 코사인 ≥ 0.999 (원본이 런타임 간 일치로 쓰는 값)."""

import json

import numpy as np
import pytest

from conftest import REFERENCE, requires_engine
from noseout import prompts

AGREEMENT = 0.999

TEXTS = {
    "query": prompts.query("payment declined error"),
    "query-bn": prompts.query("ইলিশ মাছ রান্না"),
    "query-ar": prompts.query("متى تغلق المكتبة"),
    "document": prompts.document(
        "Flight to Tokyo Haneda on November 3. Then the train to Kamakura to see the Great Buddha, "
        "and back for dinner in Shibuya.", title="Tokyo itinerary"),
    "document-long": prompts.document(" ".join(
        f"Note {n}: the review moved to room {n % 7 + 2} because the projector in the main hall failed again, "
        "and the team agreed to send the slides the night before." for n in range(1, 61))),
}
IMAGES = {"screen-280": ("screen.png", 280), "photo-140": ("photo.jpg", 140), "frame-70": ("photo.jpg", 70)}

pytestmark = requires_engine


@pytest.fixture(scope="module")
def reference():
    return {k: np.asarray(v, np.float32) for k, v in
            json.loads((REFERENCE / "vectors.json").read_text(encoding="utf-8"))["vectors"].items()}


@pytest.fixture(scope="module")
def made(embedder):
    out = dict(zip(TEXTS, embedder.embed_texts(list(TEXTS.values()))))
    for name, (file, budget) in IMAGES.items():
        out[name] = embedder.embed_image(str(REFERENCE / file), budget)
    out["speech"] = embedder.embed_audio(str(REFERENCE / "speech.wav"))
    return out


@pytest.mark.parametrize("name", [*TEXTS, *IMAGES, "speech"])
def test_matches_reference(made, reference, record, name):
    vector = made[name]
    assert vector.shape == (768,)
    assert abs(np.linalg.norm(vector) - 1) < 1e-4
    cos = float(np.dot(vector, reference[name]))
    record.setdefault("reference_cosine", {})[name] = round(cos, 6)
    assert cos >= AGREEMENT, f"{name}: cosine {cos:.6f} with the macOS reference"
