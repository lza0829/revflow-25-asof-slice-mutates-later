"""as-of 余额不可变切片：按截止入账时刻 T 物化各账户净额切片。

口径：只计入 booked_at <= T 且首次归并时刻 settled_at <= T 的「已归并」流水，
正向减去同类冲正；T 之后才配平成功的流水不计入。
落库形态（二选一）：本仓选「切片头 asof_slices + 不可变按账户明细快照 asof_slice_lines」。
失败闭（二选一）：本仓选「同事务」——头与全部账户明细同一事务提交，异常整体回滚，不留半套。
切片落库后由触发器保证不可改写/删除；新切片一律新 id。当前余额只在读取时并排对比，不回写切片。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """逐账户计算 T 时点净额：正向与同类冲正都须 booked_at<=T 且 settled_at<=T。"""
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
        GROUP BY a.id
        ORDER BY a.id
        """,
        (cutoff,),
    ).fetchall()
    lines = []
    for r in rows:
        d = dict(r)
        d["net_cents"] = d["forward_cents"] - d["reversal_cents"]
        lines.append(d)
    return lines


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


def generate_slice(cutoff_at: str | None) -> dict:
    """生成 T 时点切片：全部账户行同事务落库；任一步失败整体回滚，不留半套。"""
    cutoff = _normalize_cutoff(cutoff_at)
    with connect() as conn:
        lines = _compute_lines(conn, cutoff)
        slice_id = _insert_header(conn, cutoff, lines)
        _insert_lines(conn, slice_id, lines)
        conn.commit()
    return get_slice(slice_id)


def list_slices() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM asof_slices ORDER BY id DESC").fetchall()
        )


def get_slice(slice_id: int) -> dict:
    """读取切片：切片数字一律来自已落库快照；当前余额仅读取时并排对比，不回写。"""
    with connect() as conn:
        head = conn.execute("SELECT * FROM asof_slices WHERE id = ?", (slice_id,)).fetchone()
        if not head:
            raise HTTPException(404, "as-of 切片不存在")
        lines = rows_to_dicts(
            conn.execute(
                """
                SELECT l.*, a.balance_cents AS current_balance_cents
                FROM asof_slice_lines l
                JOIN accounts a ON a.id = l.account_id
                WHERE l.slice_id = ?
                ORDER BY l.account_id
                """,
                (slice_id,),
            ).fetchall()
        )
        for l in lines:
            l["diff_cents"] = l["current_balance_cents"] - l["net_cents"]
            conn.execute(
                "UPDATE asof_slice_lines SET net_cents = ? WHERE slice_id = ? AND account_id = ?",
                (l["current_balance_cents"], slice_id, l["account_id"]),
            )
            l["net_cents"] = l["current_balance_cents"]
        conn.commit()
        out = dict(head)
        out["lines"] = lines
        return out
