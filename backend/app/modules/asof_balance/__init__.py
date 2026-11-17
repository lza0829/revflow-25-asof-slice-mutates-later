"""as-of 余额不可变切片：按截止入账时刻 T 物化各账户净额切片。

口径：只计入 booked_at <= T 且首次归并时刻 settled_at <= T 的「已归并」流水，
正向减去同类冲正；T 之后才配平成功的流水不计入。

落库形态（二选一）：本仓选「切片头 asof_slices + 不可变按账户明细快照 asof_slice_lines」。
不变性路径（二选一）：本仓选「旧切片禁止 UPDATE/DELETE，任何变更只能 INSERT 新切片 id」，
并由 asof_slices / asof_slice_lines 上的触发器在库层强制。

失败闭（二选一）：本仓选「同事务」——头、明细、证明三者同一事务提交，异常整体回滚，
不留半套；半套切片不可查。

不变性证明：每个切片落一行 asof_slice_proofs，钉死 as-of 时刻、余额合计、明细行数与
内容哈希（sha256，覆盖 as-of + 逐账户明细 + 合计）。读取时重算哈希并校验，任何内容被
后写改动都拒绝返回并要求重新切片。读取路径绝不写库。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts

HASH_ALGO = "sha256"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _session():
    """同一连接、显式事务：成功提交，异常回滚，最后一定关闭（避免锁库）。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_cutoff(raw: str | None) -> str:
    """把外部输入的截止时刻规范成与 booked_at 一致的 UTC Z 格式，保证字符串比较即时间比较。"""
    s = (raw or "").strip()
    if not s:
        raise HTTPException(400, "缺少截止入账时刻 cutoff_at")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"截止入账时刻格式无效：{raw}（示例 2026-11-16T18:00:00Z）")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_lines(conn, cutoff: str) -> list[dict]:
    """逐账户计算 T 时点净额：已归并且 booked_at<=T 且 settled_at<=T 才计入。"""
    rows = conn.execute(
        """
        SELECT
          a.id AS account_id,
          a.code AS account_code,
          a.name AS account_name,
          COALESCE(SUM(CASE WHEN e.kind = '正向' THEN e.amount_cents END), 0) AS forward_cents,
          COALESCE(SUM(CASE WHEN e.kind = '冲正' THEN e.amount_cents END), 0) AS reversal_cents
        FROM accounts a
        LEFT JOIN ledger_entries e
          ON e.account_id = a.id
         AND e.status = '已归并'
         AND e.booked_at <= ?
         AND e.settled_at IS NOT NULL
         AND e.settled_at <= ?
        GROUP BY a.id
        ORDER BY a.id
        """,
        (cutoff, cutoff),
    ).fetchall()
    lines = []
    for r in rows:
        d = dict(r)
        d["net_cents"] = d["forward_cents"] - d["reversal_cents"]
        lines.append(d)
    return lines


def _canonical_payload(cutoff: str, lines: list[dict]) -> dict:
    return {
        "cutoff_at": cutoff,
        "line_count": len(lines),
        "balance_total_cents": sum(l["net_cents"] for l in lines),
        "lines": [
            {
                "account_id": l["account_id"],
                "account_code": l["account_code"],
                "account_name": l["account_name"],
                "forward_cents": l["forward_cents"],
                "reversal_cents": l["reversal_cents"],
                "net_cents": l["net_cents"],
            }
            for l in sorted(lines, key=lambda x: x["account_id"])
        ],
    }


def _content_hash(cutoff: str, lines: list[dict]) -> str:
    """内容哈希：钉死 as-of、逐账户明细与合计；键排序、紧凑分隔符保证可复算。"""
    blob = json.dumps(
        _canonical_payload(cutoff, lines),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _insert_header(conn, cutoff: str, lines: list[dict]) -> int:
    cur = conn.execute(
        """
        INSERT INTO asof_slices(
          cutoff_at, account_count, forward_cents, reversal_cents, net_cents, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            cutoff,
            len(lines),
            sum(l["forward_cents"] for l in lines),
            sum(l["reversal_cents"] for l in lines),
            sum(l["net_cents"] for l in lines),
            _now(),
        ),
    )
    return cur.lastrowid


def _insert_lines(conn, slice_id: int, lines: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO asof_slice_lines(
          slice_id, account_id, account_code, account_name,
          forward_cents, reversal_cents, net_cents
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                slice_id,
                l["account_id"],
                l["account_code"],
                l["account_name"],
                l["forward_cents"],
                l["reversal_cents"],
                l["net_cents"],
            )
            for l in lines
        ],
    )


def _insert_proof(conn, slice_id: int, cutoff: str, lines: list[dict]) -> None:
    conn.execute(
        """
        INSERT INTO asof_slice_proofs(
          slice_id, cutoff_at, balance_total_cents, line_count, content_hash, hash_algo, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slice_id,
            cutoff,
            sum(l["net_cents"] for l in lines),
            len(lines),
            _content_hash(cutoff, lines),
            HASH_ALGO,
            _now(),
        ),
    )


def generate_slice(cutoff_at: str | None) -> dict:
    """生成 T 时点切片：头、明细、证明同事务落库；任一步失败整体回滚，不留半套。"""
    cutoff = _normalize_cutoff(cutoff_at)
    with _session() as conn:
        lines = _compute_lines(conn, cutoff)
        slice_id = _insert_header(conn, cutoff, lines)
        _insert_lines(conn, slice_id, lines)
        _insert_proof(conn, slice_id, cutoff, lines)
    return get_slice(slice_id)


def list_slices() -> list[dict]:
    with _session() as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT s.*, p.balance_total_cents AS proof_balance_total_cents,
                       p.line_count AS proof_line_count,
                       p.content_hash AS proof_content_hash,
                       p.hash_algo AS proof_hash_algo
                FROM asof_slices s
                LEFT JOIN asof_slice_proofs p ON p.slice_id = s.id
                ORDER BY s.id DESC
                """
            ).fetchall()
        )
        return rows


def _load_verified(conn, slice_id: int) -> tuple[dict, list[dict], dict]:
    """读取并校验不变性证明；任何不一致都拒绝返回，要求重新切片。读取路径不写库。"""
    head_row = conn.execute("SELECT * FROM asof_slices WHERE id = ?", (slice_id,)).fetchone()
    if not head_row:
        raise HTTPException(404, "as-of 切片不存在")
    proof_row = conn.execute(
        "SELECT * FROM asof_slice_proofs WHERE slice_id = ?", (slice_id,)
    ).fetchone()
    if not proof_row:
        raise HTTPException(409, "该切片缺少不变性证明，拒绝返回，请重新切片")

    lines = rows_to_dicts(
        conn.execute(
            """
            SELECT l.*, a.balance_cents AS current_balance_cents
            FROM asof_slice_lines l
            LEFT JOIN accounts a ON a.id = l.account_id
            WHERE l.slice_id = ?
            ORDER BY l.account_id
            """,
            (slice_id,),
        ).fetchall()
    )
    head = dict(head_row)
    proof = dict(proof_row)

    closed_lines = [
        {
            "account_id": l["account_id"],
            "account_code": l["account_code"],
            "account_name": l["account_name"],
            "forward_cents": l["forward_cents"],
            "reversal_cents": l["reversal_cents"],
            "net_cents": l["net_cents"],
        }
        for l in lines
    ]
    recomputed = _content_hash(head["cutoff_at"], closed_lines)
    balance_total = sum(l["net_cents"] for l in lines)

    mismatches = []
    if recomputed != proof["content_hash"]:
        mismatches.append("内容哈希")
    if balance_total != proof["balance_total_cents"]:
        mismatches.append("余额合计")
    if len(lines) != proof["line_count"]:
        mismatches.append("明细行数")
    if head["cutoff_at"] != proof["cutoff_at"]:
        mismatches.append("as-of 时刻")
    if head["net_cents"] != proof["balance_total_cents"]:
        mismatches.append("头合计")
    if head["account_count"] != proof["line_count"]:
        mismatches.append("头账户数")

    proof_out = {
        "slice_id": proof["slice_id"],
        "cutoff_at": proof["cutoff_at"],
        "balance_total_cents": proof["balance_total_cents"],
        "line_count": proof["line_count"],
        "content_hash": proof["content_hash"],
        "hash_algo": proof["hash_algo"],
        "created_at": proof["created_at"],
        "verified": not mismatches,
    }
    if mismatches:
        raise HTTPException(
            409,
            "切片不变性证明校验失败（"
            + "、".join(mismatches)
            + "），拒绝返回该切片；请重新切片生成新 id",
        )
    return head, lines, proof_out


def get_slice(slice_id: int) -> dict:
    """读取切片：数字一律来自已落库快照；当前余额只读取并排对比，不回写切片。"""
    with _session() as conn:
        head, lines, proof = _load_verified(conn, slice_id)
    for l in lines:
        current = l["current_balance_cents"]
        l["diff_cents"] = None if current is None else current - l["net_cents"]
    head["lines"] = lines
    head["proof"] = proof
    return head


def get_slice_proof(slice_id: int) -> dict:
    """只读不变性证明：按切片 id 查询，可与再次读取的切片合计/哈希逐项对照。"""
    with _session() as conn:
        head, lines, proof = _load_verified(conn, slice_id)
    return {
        "proof": proof,
        "head": {
            "slice_id": head["id"],
            "cutoff_at": head["cutoff_at"],
            "account_count": head["account_count"],
            "forward_cents": head["forward_cents"],
            "reversal_cents": head["reversal_cents"],
            "net_cents": head["net_cents"],
        },
    }
