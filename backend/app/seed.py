from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  balance_cents INTEGER NOT NULL DEFAULT 0,
  last_settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  biz_ref TEXT NOT NULL DEFAULT '',
  target_entry_key TEXT,
  booked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待归并',
  note TEXT NOT NULL DEFAULT '',
  settle_run_id INTEGER,
  settled_at TEXT
);
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  settle_run_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settle_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS verify_reports (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  ledger_net_cents INTEGER NOT NULL,
  balance_sum_cents INTEGER NOT NULL,
  quarantine_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- as-of 余额切片：切片头 + 不可变按账户明细快照（落库形态二选一，本仓选此形态）
CREATE TABLE IF NOT EXISTS asof_slices (
  id INTEGER PRIMARY KEY,
  cutoff_at TEXT NOT NULL,
  account_count INTEGER NOT NULL,
  forward_cents INTEGER NOT NULL,
  reversal_cents INTEGER NOT NULL,
  net_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asof_slice_lines (
  id INTEGER PRIMARY KEY,
  slice_id INTEGER NOT NULL REFERENCES asof_slices(id),
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  account_code TEXT NOT NULL,
  account_name TEXT NOT NULL,
  forward_cents INTEGER NOT NULL,
  reversal_cents INTEGER NOT NULL,
  net_cents INTEGER NOT NULL,
  UNIQUE(slice_id, account_id)
);
-- 不变性证明实体：每个切片一行，钉死 as-of、余额合计、明细行数与内容哈希
CREATE TABLE IF NOT EXISTS asof_slice_proofs (
  slice_id INTEGER PRIMARY KEY REFERENCES asof_slices(id),
  cutoff_at TEXT NOT NULL,
  balance_total_cents INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  hash_algo TEXT NOT NULL DEFAULT 'sha256',
  created_at TEXT NOT NULL
);
-- 不变性路径：「旧切片禁止 UPDATE/DELETE，任何变更只能 INSERT 新切片 id」
CREATE TRIGGER IF NOT EXISTS trg_asof_slices_no_update
BEFORE UPDATE ON asof_slices
BEGIN
  SELECT RAISE(ABORT, 'as-of 切片不可改写：任何变更只能以新 id 追加新切片');
END;
CREATE TRIGGER IF NOT EXISTS trg_asof_slices_no_delete
BEFORE DELETE ON asof_slices
BEGIN
  SELECT RAISE(ABORT, 'as-of 切片不可删除：任何变更只能以新 id 追加新切片');
END;
CREATE TRIGGER IF NOT EXISTS trg_asof_slice_lines_no_update
BEFORE UPDATE ON asof_slice_lines
BEGIN
  SELECT RAISE(ABORT, 'as-of 切片明细不可改写：任何变更只能以新 id 追加新切片');
END;
CREATE TRIGGER IF NOT EXISTS trg_asof_slice_lines_no_delete
BEFORE DELETE ON asof_slice_lines
BEGIN
  SELECT RAISE(ABORT, 'as-of 切片明细不可删除：任何变更只能以新 id 追加新切片');
END;
CREATE TRIGGER IF NOT EXISTS trg_asof_slice_proofs_no_update
BEFORE UPDATE ON asof_slice_proofs
BEGIN
  SELECT RAISE(ABORT, 'as-of 切片证明不可改写：任何变更只能以新 id 追加新切片');
END;
CREATE TRIGGER IF NOT EXISTS trg_asof_slice_proofs_no_delete
BEFORE DELETE ON asof_slice_proofs
BEGIN
  SELECT RAISE(ABORT, 'as-of 切片证明不可删除：任何变更只能以新 id 追加新切片');
END;

"""


def _migrate(conn) -> None:
    """旧库补列：ledger_entries.settled_at 记录首次变为「已归并」的时刻。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ledger_entries)")}
    if "settled_at" not in cols:
        conn.execute("ALTER TABLE ledger_entries ADD COLUMN settled_at TEXT")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if n:
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for a in data["accounts"]:
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, ?, 0)",
                (a["id"], a["code"], a["name"], a["currency"]),
            )
        for e in data["forward_entries"]:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                """,
                (
                    e["id"],
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        # 冲正 id 与正向可能重叠，用独立自增：先取 max
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger_entries").fetchone()["m"]
        for i, e in enumerate(data["reversal_entries"], start=1):
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
                """,
                (
                    max_id + i,
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e.get("target_entry_key"),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        conn.commit()
