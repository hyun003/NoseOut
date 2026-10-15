"""명령줄 도구 (2단계 검증용, UI는 5단계).

  python -m noseout [--db 색인.sqlite] index <폴더> [<폴더> ...]
  python -m noseout [--db 색인.sqlite] search "<검색어>" [--limit 10]
  python -m noseout [--db 색인.sqlite] status
  python -m noseout app      검색 창·트레이 (설정·색인: %LOCALAPPDATA%\\NoseOut, NOSEOUT_HOME으로 바꿈)

(--db는 명령 이름 앞에 쓴다.)

--db를 주지 않으면 %LOCALAPPDATA%\\NoseOut\\index.sqlite 를 쓴다(옛 DigUpWin 폴더는 paths.py가 옮긴다).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import engine, paths, prompts
from .indexer import IncompatibleIndex, Indexer
from .searcher import Searcher, strong_count
from .store import IndexStore


def default_db() -> Path:
    return paths.home() / "index.sqlite"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="noseout")
    parser.add_argument("--db", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("index")
    index.add_argument("folders", nargs="+")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--json", action="store_true")
    sub.add_parser("status")
    sub.add_parser("app")   # 검색 창·트레이 (5단계)
    args = parser.parse_args(argv)
    if args.command == "app":
        from .app import main as app_main

        return app_main()
    db = args.db or default_db()

    if args.command == "index":
        with IndexStore(db) as store:
            embedder = None

            def start():
                nonlocal embedder
                embedder = engine.Embedder()
                return embedder

            try:
                indexer = Indexer(store, start)
                versions = indexer.versions
                if versions.requeued:
                    print(f"index settings changed ({', '.join(versions.changed_components)}): re-reading "
                          f"{versions.requeued} files; their previous entries stay searchable until then")
                report = indexer.sync(args.folders)
            except IncompatibleIndex as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
            finally:
                if embedder:
                    embedder.close()
            print(f"added {report.added}, changed {report.changed}, removed {report.removed}, "
                  f"unchanged {report.unchanged}, {report.seconds:.1f} s")
            for path, result in report.results.items():
                print(f"  {result:40.40}  {path}")
            for path, reason in report.skipped.items():
                print(f"  skip {reason:35.35}  {path}")
            print(json.dumps(store.counts(), ensure_ascii=False))
        return 0

    if args.command == "search":
        if not db.exists():
            print(f"no index at {db}", file=sys.stderr)
            return 1
        with IndexStore(db) as store, engine.Embedder(engine.ServerConfig(media_model=None)) as embedder:
            vector = embedder.embed_texts([prompts.query(args.query)])[0]
            hits = Searcher(store).search(args.query, vector, limit=args.limit)
        if args.json:
            print(json.dumps([h.__dict__ for h in hits], ensure_ascii=False, indent=2))
            return 0
        strong = strong_count(hits)
        for rank, h in enumerate(hits, 1):
            where = f" p.{int(h.loc)}" if h.loc and h.segment in ("page", "page_image") else ""
            if h.loc is not None and h.segment in ("audio", "frame"):
                where = f" @{int(h.loc) // 60}:{int(h.loc) % 60:02d}"
            fold = "" if rank <= strong else "  (weaker)"
            print(f"{rank:2}. {h.score:5.2f} [{h.kind}{where}] {h.path}{fold}")
            if h.excerpt:
                print(f"      {h.excerpt[:160]}")
        return 0

    with IndexStore(db) as store:
        print(json.dumps(store.counts(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
