"""setsScale 켬/끔 비교: 검색 정확도(R@1, R@3, MRR, 쪽·구간), 검색 속도, 질의별 상위 파일.

설정은 이 스크립트 안에서만 바꾼다(제품 코드의 기본값은 원본과 같은 '켬'). 색인은 한 번 만들고 두 설정이 같은 색인을 쓴다.

usage: .venv\\Scripts\\python scripts\\compare_sets_scale.py [--evals evals\\holdout.json] [--out results\\holdout-setsscale.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from noseout import engine, prompts, searcher as searcher_module  # noqa: E402
from noseout.indexer import Indexer  # noqa: E402
from noseout.store import IndexStore  # noqa: E402
from eval_search import evaluate, load  # noqa: E402

ORIGINAL_RELOAD = searcher_module.Searcher.reload


def reload_without_scale(self):
    ORIGINAL_RELOAD(self)
    self.row_sets_scale = np.ones(len(self.row_file), bool)


MODES = {"on": ORIGINAL_RELOAD, "off": reload_without_scale}


def timing(store, embedder, evals, repeat: int = 20) -> dict:
    """질의 임베딩을 뺀 검색 시간(뜻 점수 + FTS + 순위), 질의마다 repeat번의 중앙값."""
    s = searcher_module.Searcher(store)
    vectors = embedder.embed_texts([prompts.query(q["q"]) for q in evals["queries"]])
    per_query = []
    for q, v in zip(evals["queries"], vectors):
        runs = []
        for _ in range(repeat):
            started = time.perf_counter()
            s.search(q["q"], v)
            runs.append((time.perf_counter() - started) * 1000)
        per_query.append(statistics.median(runs))
    return {"median_ms": round(statistics.median(per_query), 2), "max_ms": round(max(per_query), 2),
            "vectors": int(len(s.row_file)), "scale_rows": int(s.row_sets_scale.sum())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evals", type=Path, default=ROOT / "evals" / "holdout.json")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "holdout-setsscale.json")
    args = parser.parse_args()
    evals = load(args.evals)
    db = args.db or ROOT / ".index" / f"{args.evals.stem}.sqlite"
    out = {"evals": args.evals.name, "modes": {}}
    with engine.Embedder() as embedder, IndexStore(db) as store:
        report = Indexer(store, embedder).sync([ROOT / evals["base"]])
        out["index"] = {"files": store.counts()["files"], "vectors": store.counts()["vectors"],
                        "seconds": round(report.seconds, 1)}
        for mode, reload in MODES.items():
            searcher_module.Searcher.reload = reload
            out["modes"][mode] = {"hybrid": evaluate(store, embedder, evals),
                                  "meaning_only": evaluate(store, embedder, evals, words=False),
                                  "speed": timing(store, embedder, evals)}
        searcher_module.Searcher.reload = ORIGINAL_RELOAD
    # 질의별로 두 설정의 상위 3개가 달라진 것
    changed = []
    for on, off in zip(out["modes"]["on"]["hybrid"]["rows"], out["modes"]["off"]["hybrid"]["rows"]):
        if on["top"] != off["top"] or on["rank"] != off["rank"]:
            changed.append({"q": on["q"], "expect": on["expect"], "on": {"rank": on["rank"], "top": on["top"]},
                            "off": {"rank": off["rank"], "top": off["top"]}})
    out["changed_queries"] = changed
    args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print("index:", out["index"])
    for mode, m in out["modes"].items():
        print(f"== setsScale {mode}: hybrid {m['hybrid']['overall']}")
        print(f"   meaning-only {m['meaning_only']['overall']}")
        print(f"   speed {m['speed']}")
        for tag, s in m["hybrid"]["by_tag"].items():
            print(f"     {tag:13} {s}")
    print(f"queries whose top-3 or rank changed: {len(changed)}")
    for c in changed:
        print(f"   {c['q']}\n      on  {c['on']}\n      off {c['off']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
