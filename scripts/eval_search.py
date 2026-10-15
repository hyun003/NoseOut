"""검색 평가: evals/*.json 질의로 R@1, R@3, MRR, 가리킨 위치(쪽·구간)의 정확도를 잰다. 원본 `digup eval`과 같은 지표.

usage: .venv\\Scripts\\python scripts\\eval_search.py [--db .index\\test_data.sqlite] [--evals evals\\test_data.json]
       (색인이 없으면 test_data를 먼저 색인한다)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from noseout import engine, prompts  # noqa: E402
from noseout.searcher import Searcher  # noqa: E402
from noseout.store import IndexStore  # noqa: E402


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(store: IndexStore, embedder, evals: dict, words: bool = True, limit: int = 10) -> dict:
    """`words=False`면 검색어의 단어(키워드)를 쓰지 않고 뜻으로만 찾는다."""
    base = (ROOT / evals["base"]).resolve()
    queries = evals["queries"]
    searcher = Searcher(store)
    vectors = embedder.embed_texts([prompts.query(q["q"]) for q in queries])
    rows = []
    for q, vector in zip(queries, vectors):
        hits = searcher.search(q["q"] if words else "", vector, limit=limit)
        names = [Path(h.path).relative_to(base).as_posix() for h in hits]
        rank = names.index(q["expect"]) + 1 if q["expect"] in names else None
        hit = hits[rank - 1] if rank else None
        place = None
        if hit and ("page" in q or "contains" in q):
            if "page" in q:
                place = hit.loc is not None and int(hit.loc) == q["page"]
            else:
                text = store.db.execute("SELECT coalesce(original, body) FROM fts WHERE rowid = ?",
                                        (hit.segment_id,)).fetchone()
                place = bool(text and q["contains"] in text[0])
        rows.append({"q": q["q"], "expect": q["expect"], "tag": q["tag"], "rank": rank, "place_ok": place,
                     "top": names[:3], "hits": len(hits),
                     "z": round(hit.z, 2) if hit and hit.z is not None else None,
                     "loc": hit.loc if hit else None})
    return {"words": words, "overall": summarize(rows), "by_tag": by_tag(rows), "rows": rows}


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    located = [r for r in rows if r["place_ok"] is not None]
    return {
        "n": n,
        "r1": round(sum(r["rank"] == 1 for r in rows) / n, 3),
        "r3": round(sum(r["rank"] is not None and r["rank"] <= 3 for r in rows) / n, 3),
        "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 3),
        "place_ok": f"{sum(bool(r['place_ok']) for r in located)}/{len(located)}" if located else None,
        "hits_per_query": round(sum(r.get("hits", 0) for r in rows) / n, 2),   # 정답 말고 붙는 결과가 많으면 잡음
    }


def by_tag(rows: list[dict]) -> dict:
    groups = defaultdict(list)
    for r in rows:
        for tag in r["tag"]:
            groups[tag].append(r)
    return {tag: summarize(rs) for tag, rs in sorted(groups.items())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / ".index" / "test_data.sqlite")
    parser.add_argument("--evals", type=Path, default=ROOT / "evals" / "test_data.json")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    evals = load(args.evals)
    with engine.Embedder() as embedder, IndexStore(args.db) as store:
        if store.counts()["vectors"] == 0:
            from noseout.indexer import Indexer

            Indexer(store, embedder).sync([ROOT / evals["base"]])
        results = {"hybrid": evaluate(store, embedder, evals), "meaning_only": evaluate(store, embedder, evals, False)}
    for mode, result in results.items():
        print(f"== {mode}: {result['overall']}")
        for tag, s in result["by_tag"].items():
            print(f"   {tag:14} {s}")
        for r in result["rows"]:
            if r["rank"] != 1 or r["place_ok"] is False:
                print(f"   miss rank={r['rank']} place={r['place_ok']} z={r['z']} top={r['top'][:2]}  {r['q']}")
    if args.out:
        args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
