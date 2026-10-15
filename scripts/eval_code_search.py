"""6단계 코드 검색 평가: 합성 코드 저장소를 .tmp/code-eval에 만들고 실제 엔진으로 코드 색인 → "code:" 검색.
자연어 질의(파일·함수 이름에 단서 없음)는 맞는 파일이 몇 위인지, 이름 질의는 정의 줄로 여는지 본다.
기존 .index와 사용자 파일은 쓰지 않는다. 결과: results/stage6-code.json

    .venv\\Scripts\\python scripts\\eval_code_search.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import PySide6.QtCore  # noqa: E402,F401  (winrt보다 먼저)

from noseout import code as code_files  # noqa: E402
from noseout import engine, prompts  # noqa: E402
from noseout.indexer import Indexer  # noqa: E402
from noseout.searcher import Searcher  # noqa: E402
from noseout.store import IndexStore  # noqa: E402

WORK = ROOT / ".tmp" / "code-eval"

FILES = {
    "shop/payments/client.py": '''import random
import time


class PaymentClient:
    def __init__(self, session, attempts=5):
        self.session = session
        self.attempts = attempts

    def charge(self, card, cents):
        for attempt in range(self.attempts):
            try:
                return self.session.post("/charges", json={"card": card, "amount": cents})
            except TimeoutError:
                # wait longer after every failure, plus a little randomness
                time.sleep(0.2 * 2 ** attempt + random.random() / 10)
        raise RuntimeError("payment service unavailable")
''',
    "shop/web/csv.ts": '''export function splitRow(line: string): string[] {
  const cells: string[] = [];
  let current = "";
  let quoted = false;
  for (const ch of line) {
    if (ch === '"') quoted = !quoted;
    else if (ch === "," && !quoted) { cells.push(current); current = ""; }
    else current += ch;
  }
  cells.push(current);
  return cells;
}
''',
    "shop/auth/tokens.go": '''package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
)

// Sign returns the message authentication code for a session id.
func Sign(secret []byte, session string) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(session))
	return hex.EncodeToString(mac.Sum(nil))
}

func Verify(secret []byte, session, signature string) bool {
	return hmac.Equal([]byte(Sign(secret, session)), []byte(signature))
}
''',
    "shop/cache/lru.rs": '''use std::collections::HashMap;

pub struct Recent<V> {
    capacity: usize,
    order: Vec<String>,
    items: HashMap<String, V>,
}

impl<V> Recent<V> {
    pub fn put(&mut self, key: String, value: V) {
        if self.items.len() == self.capacity {
            let oldest = self.order.remove(0);
            self.items.remove(&oldest);
        }
        self.order.push(key.clone());
        self.items.insert(key, value);
    }
}
''',
    "shop/images/resize.py": '''from PIL import Image


def make_thumbnail(path, out, longest=320):
    """Shrink a picture so its longest side fits, keeping the aspect ratio."""
    with Image.open(path) as image:
        image.thumbnail((longest, longest))
        image.save(out, quality=85)
''',
    "shop/db/migrate.sql": '''CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    total_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX orders_customer ON orders(customer_id);
''',
    "shop/scripts/backup.ps1": '''param([string]$Target = "D:\\Backups")
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
Compress-Archive -Path ".\\data\\*" -DestinationPath (Join-Path $Target "shop-$stamp.zip")
Get-ChildItem $Target -Filter "shop-*.zip" | Sort-Object LastWriteTime | Select-Object -SkipLast 7 | Remove-Item
''',
    "shop/mail/templates.js": '''function renderReceipt(order) {
  const lines = order.items.map(item => `${item.name} x${item.quantity}: ${item.price}`);
  return `Thank you for your order #${order.id}\\n` + lines.join("\\n") + `\\nTotal: ${order.total}`;
}
module.exports = { renderReceipt };
''',
    "shop/stats/mean.py": '''def moving_average(values, window=7):
    """Smooth a daily series: the mean of each value and the ones before it."""
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1):i + 1]
        out.append(sum(chunk) / len(chunk))
    return out
''',
    "shop/README.md": "# Shop\n\nA small web shop: payments, receipts by email, nightly backups.\n",
}

# (질의, 맞는 파일, 열어야 할 줄 또는 None)
QUERIES = [
    ("retry a payment with exponential backoff", "shop/payments/client.py", None),
    ("parse a comma separated line with quoted fields", "shop/web/csv.ts", None),
    ("sign a session id with HMAC SHA-256", "shop/auth/tokens.go", None),
    ("evict the oldest entry when the cache is full", "shop/cache/lru.rs", None),
    ("make a smaller preview image keeping proportions", "shop/images/resize.py", None),
    ("database table for customer orders", "shop/db/migrate.sql", None),
    ("zip the data folder and keep the last seven archives", "shop/scripts/backup.ps1", None),
    ("email text listing the items bought", "shop/mail/templates.js", None),
    ("smooth a time series with a sliding window mean", "shop/stats/mean.py", None),
    ("결제가 실패하면 점점 오래 기다렸다가 다시 시도", "shop/payments/client.py", None),
    ("오래된 백업 압축 파일 지우기", "shop/scripts/backup.ps1", None),
    ("make_thumbnail", "shop/images/resize.py", 4),
    ("moving_average", "shop/stats/mean.py", 1),
    ("renderReceipt", "shop/mail/templates.js", 1),
]


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    repo = WORK / "repos"
    for relative, text in FILES.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    # 실제 git 저장소 (제목 = 저장소 기준 경로, git 무시 규칙 경로도 함께 지난다)
    subprocess.run(["git", "init", "-q"], cwd=repo / "shop", check=True)
    rows = []
    with engine.Embedder() as embedder, IndexStore(WORK / "code.sqlite") as store:
        started = time.perf_counter()
        report = Indexer(store, embedder, code=True).sync([repo])
        index_seconds = time.perf_counter() - started
        searcher = Searcher(store, code=True)
        for query, expected, line in QUERIES:
            vector = embedder.embed_texts([prompts.code_query(query)])[0]
            hits = searcher.search(query, vector, limit=10)
            names = [Path(h.path).relative_to(repo).as_posix() for h in hits]
            rank = names.index(expected) + 1 if expected in names else None
            focus = hits[rank - 1].focus if rank else None
            rows.append({"query": query, "expected": expected, "rank": rank, "top": names[:3],
                         "line_expected": line, "line": focus, "location": code_files.language(expected)})
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    meaning = [r for r in rows if r["line_expected"] is None]
    names = [r for r in rows if r["line_expected"] is not None]
    out = {"measured": time.strftime("%Y-%m-%d %H:%M"), "files": len(FILES),
           "indexed": sum(v.startswith("done") for v in report.results.values()),
           "index_seconds": round(index_seconds, 2),
           "meaning_queries": len(meaning), "meaning_r1": sum(r["rank"] == 1 for r in meaning),
           "meaning_r3": sum(bool(r["rank"]) and r["rank"] <= 3 for r in meaning),
           "name_queries": len(names), "name_r1": sum(r["rank"] == 1 for r in names),
           "name_line_correct": sum(r["line"] == r["line_expected"] for r in names), "rows": rows}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "stage6-code.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
