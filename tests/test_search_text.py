from noseout import search_text, searcher


def test_normalized():
    assert search_text.normalized("ＡＢＣ１２３") == "ABC123"           # 전각 → ASCII
    assert search_text.normalized("a‍b­c") == "abc"            # 결합자, soft hyphen
    assert search_text.normalized("أحمد") == "احمد"  # 아랍어 alef


def test_inflects():
    assert search_text.inflects("청구서")
    assert search_text.inflects("কুমির")
    assert not search_text.inflects("invoice")


def test_has_words():
    assert search_text.has_words("The tenant may keep one cat.")
    assert search_text.has_words("김치찌개 레시피 2인분")
    assert not search_text.has_words("12.5 33.1 98.0 41.7 66.2 10.0")


def test_words_and_terms():
    assert search_text.words("INV-4821, 청구서를 보여줘!") == ["inv", "4821", "청구서를", "보여줘"]
    assert searcher.terms("find the photo of a stop sign") == ["stop", "sign"]
    assert searcher.terms("엘리베이터 점검 사진") == ["엘리베이터", "점검"]


def test_patterns_prefix_for_korean():
    assert searcher.patterns(["lease", "청구서"]) == ['"lease"', '"청구서"*']
    assert searcher.patterns(["lea"], prefix_last=True) == ['"lea"*']


def test_lookup_and_hints():
    assert searcher.is_lookup(["k7q2lm"]) and searcher.is_lookup(["4815162342"])
    assert not searcher.is_lookup(["inv", "4821"])  # 원본과 같음: 글자만 있는 조각이 섞이면 아님
    assert searcher.kind_hints("screenshot of the error") == {"screenshot"}
    assert searcher.kind_hints("바다 사진을 찾아줘") == {"image"}   # 조사가 붙어도
    assert searcher.kind_hints("회의 문서") == {"doc"}


def test_excerpt():
    assert search_text.excerpt("a  b\n\nc") == "a b c"
    assert search_text.excerpt("x" * 300).endswith("…")


def test_korean_particles_are_stripped_from_query_words():
    # 3단계: 검색어 "비용은"이 문서의 "비용"을 찾도록 (문서 쪽 조사는 접두어 검색이 맡는다)
    assert searcher.terms("주차 비용은 어디서") == ["주차", "비용", "어디서"]
    assert searcher.terms("청구서를 보여줘") == ["청구서"]
    assert searcher.strip_particle("차를") == "차를"        # 한 글자만 남으면 떼지 않는다
    assert searcher.strip_particle("invoice") == "invoice"
