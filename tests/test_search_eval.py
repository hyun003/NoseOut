"""3단계 평가 세트(evals/test_data.json)로 검색 품질을 고정한다. 결과는 results/stage3-eval.json.

- 'meaning' 질의: 검색어의 어떤 단어도 정답 파일·폴더 이름에 없다 (아래 test_eval_set_integrity가 확인)
- words=False: 키워드를 끄고 뜻(벡터)만으로 찾는다
- 'long-pdf', 'long-doc': 첫 단계 뒤(30쪽·40청크 너머)에 있는 내용까지 맞는 쪽·구간을 가리키는지

기준은 2026-10-13 측정값(docs/progress.md)이다. 낮추려면 이유를 docs/에 적는다(AGENTS.md).
"""

import json
import sys
from pathlib import Path

import pytest

from conftest import RESULTS, ROOT, requires_engine
from noseout import search_text, searcher
from noseout.indexer import Indexer
from noseout.store import IndexStore

sys.path.insert(0, str(ROOT / "scripts"))
from eval_search import evaluate, load  # noqa: E402

EVALS = load(ROOT / "evals" / "test_data.json")


def test_eval_set_integrity(test_data):
    """평가 세트가 주장하는 것을 지키는지: 파일이 있고, 'meaning' 질의는 이름 단어와 겹치지 않는다."""
    for q in EVALS["queries"]:
        assert (test_data / q["expect"]).exists(), q["expect"]
        if "meaning" not in q["tag"]:
            continue
        names = set(search_text.words(q["expect"].replace("_", " ")))
        for term in searcher.terms(q["q"]):
            assert term not in names, f"{q['q']!r}: '{term}' is in the file name"
            if search_text.inflects(term):
                assert not any(n.startswith(term) or term.startswith(n) for n in names), (q["q"], term)


@pytest.fixture(scope="module")
def evaluated(test_data, embedder, tmp_path_factory):
    with IndexStore(tmp_path_factory.mktemp("eval") / "index.sqlite") as store:
        Indexer(store, embedder).sync([test_data])
        results = {"hybrid": evaluate(store, embedder, EVALS), "meaning_only": evaluate(store, embedder, EVALS, False)}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "stage3-eval.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return results


@requires_engine
def test_overall(evaluated):
    overall = evaluated["hybrid"]["overall"]
    assert overall["r1"] >= 0.86 and overall["r3"] >= 0.93 and overall["mrr"] >= 0.89, overall


@requires_engine
def test_queries_without_file_name_words(evaluated):
    tag = evaluated["hybrid"]["by_tag"]["meaning"]
    assert tag["r1"] >= 0.83 and tag["r3"] >= 0.91, tag


@requires_engine
def test_meaning_only(evaluated):
    overall = evaluated["meaning_only"]["overall"]
    assert overall["r1"] >= 0.82, overall
    # 뜻만으로 찾았을 때 가리킨 쪽·구간은 모두 맞아야 한다
    rows = [r for r in evaluated["meaning_only"]["rows"] if r["place_ok"] is not None]
    assert all(r["place_ok"] for r in rows), rows


@requires_engine
def test_long_pdf_points_at_the_right_page(evaluated):
    tag = evaluated["hybrid"]["by_tag"]["long-pdf"]
    assert tag["r1"] == 1.0 and tag["place_ok"] == "5/5", tag
    # 첫 단계(30쪽) 너머의 쪽도 뜻만으로 찾는다
    deep = [r for r in evaluated["meaning_only"]["rows"] if "long-pdf" in r["tag"] and "meaning" in r["tag"]]
    assert all(r["rank"] == 1 and r["place_ok"] for r in deep), deep


@requires_engine
def test_long_documents(evaluated):
    tag = evaluated["hybrid"]["by_tag"]["long-doc"]
    assert tag["r3"] == 1.0, tag
    rows = [r for r in evaluated["hybrid"]["rows"] if "long-doc" in r["tag"] and r["rank"] == 1]
    assert all(r["place_ok"] for r in rows), rows                 # 1위면 맞는 구간을 가리킨다
    # 40청크 너머(handbook 59번째 청크, 규정 49번째 청크)의 내용도 찾는다
    korean = next(r for r in evaluated["hybrid"]["rows"] if r["q"] == "야간 근무 수당은 얼마인가")
    assert korean["rank"] == 1 and korean["place_ok"]
