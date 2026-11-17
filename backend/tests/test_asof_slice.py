from pathlib import Path
import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = str(ROOT / "backend" / "data_test_asof")
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

db_path = Path(os.environ["DATA_DIR"]) / "app.db"
if db_path.exists():
    db_path.unlink()

# 各测试模块用各自 DATA_DIR：导入前清掉已缓存的 app.*，避免串库
for _m in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

from app.main import app  # noqa: E402
from app.modules import asof_balance as asof_svc  # noqa: E402  与本模块 app 同一实例


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _db():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _slice_by_code(slice_body):
    return {l["account_code"]: l for l in slice_body["lines"]}


def test_slice_after_settle(client):
    """归并后生成切片：逐账户净额与当前余额一致（差异为 0），且 settled_at 已被归并写入。"""
    run = client.post("/api/settle-runs")
    assert run.status_code == 200, run.text

    r = client.post("/api/asof-slices", params={"cutoff_at": "2099-01-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["cutoff_at"] == "2099-01-01T00:00:00Z"
    assert s["account_count"] == 3
    lines = _slice_by_code(s)
    # 种子净额：acc1 = 43000-15000，acc2 = 18000-18000，acc3 = 50000（无原单冲正进隔离不计）
    assert lines["ACC-零售-01"]["net_cents"] == 28000
    assert lines["ACC-零售-02"]["net_cents"] == 0
    assert lines["ACC-批发-01"]["net_cents"] == 50000
    assert s["net_cents"] == 78000
    # 刚归并完：切片净额 == 当前余额，并排差异为 0
    assert all(l["diff_cents"] == 0 for l in s["lines"])
    # settled_at 已落库（否则上面的切片不可能非零）
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM ledger_entries WHERE status='已归并' AND settled_at IS NULL"
    ).fetchone()["c"]
    conn.close()
    assert n == 0


def test_slice_cutoff_filters_by_booked_and_settled(client):
    """口径：booked_at<=T 且首次归并时刻<=T 才计入；两个条件各自独立生效。"""
    conn = _db()
    rows = [
        # key, kind, 金额, booked_at, settled_at —— 期望
        ("FWD-T2-A", "正向", 1000, "2026-11-15T08:00:00Z", "2026-11-15T09:00:00Z"),  # 双条件满足→计
        ("REV-T2-A", "冲正", 400, "2026-11-15T08:30:00Z", "2026-11-15T09:30:00Z"),   # 双条件满足→计
        ("FWD-T2-B", "正向", 2000, "2026-11-16T08:00:00Z", "2026-11-16T09:00:00Z"),  # booked>T→不计
        ("FWD-T2-C", "正向", 4000, "2026-11-15T10:00:00Z", "2026-11-17T09:00:00Z"),  # booked≤T 但 settled>T→不计
    ]
    for key, kind, amt, booked, settled in rows:
        conn.execute(
            """
            INSERT INTO ledger_entries(
              entry_key, account_id, kind, amount_cents, biz_ref,
              booked_at, status, note, settled_at
            ) VALUES (?, 1, ?, ?, '', ?, '已归并', '受控时点样本', ?)
            """,
            (key, kind, amt, booked, settled),
        )
    conn.commit()
    conn.close()

    # T 取种子流水入账日之前：种子流水与 run-1 归并时刻都在 T 之后，天然排除
    r = client.post("/api/asof-slices", params={"cutoff_at": "2026-11-16T00:00:00Z"})
    assert r.status_code == 200, r.text
    lines = _slice_by_code(r.json())
    acc1 = lines["ACC-零售-01"]
    assert acc1["forward_cents"] == 1000
    assert acc1["reversal_cents"] == 400
    assert acc1["net_cents"] == 600
    assert lines["ACC-零售-02"]["net_cents"] == 0
    assert lines["ACC-批发-01"]["net_cents"] == 0


def test_slice_excludes_late_settled_and_never_rewritten(client):
    """T 之后才配平成功的流水不计入；后续归并/撤销改写当前余额，但已生成切片数字不变。"""
    T = "2027-01-01T00:00:00Z"
    s1 = client.post("/api/asof-slices", params={"cutoff_at": T}).json()
    l1 = _slice_by_code(s1)
    # 此刻全部已归并流水的 booked/settled 都 ≤T：acc1 含受控样本，acc3 为种子净额
    assert l1["ACC-零售-01"]["net_cents"] == 28000 + 1000 - 400 + 2000 + 4000
    assert l1["ACC-批发-01"]["net_cents"] == 50000

    # 模拟「T 之后才配平成功」：booked_at≤T，但首次归并时刻 >T；同时当前余额被改写
    conn = _db()
    conn.execute(
        """
        INSERT INTO ledger_entries(
          entry_key, account_id, kind, amount_cents, biz_ref,
          booked_at, status, note, settled_at
        ) VALUES ('FWD-LATE-001', 3, '正向', 7000, 'ORD-LATE',
                  '2026-12-31T23:00:00Z', '已归并', '迟到配平', '2027-02-01T00:00:00Z')
        """
    )
    conn.execute("UPDATE accounts SET balance_cents = 57000 WHERE id = 3")
    conn.commit()
    conn.close()

    s2 = client.post("/api/asof-slices", params={"cutoff_at": T}).json()
    assert s2["id"] != s1["id"]  # 新切片用新 id
    l2 = _slice_by_code(s2)
    # 同一 T 重新切片：迟到配平的 7000 不得计入，两个切片数字完全一致
    assert l2["ACC-批发-01"]["net_cents"] == 50000
    assert s2["net_cents"] == s1["net_cents"]
    assert {k: v["net_cents"] for k, v in l2.items()} == {k: v["net_cents"] for k, v in l1.items()}
    # 当前余额并排可见差异（57000 vs 50000），但切片不被覆盖
    assert l2["ACC-批发-01"]["current_balance_cents"] == 57000
    assert l2["ACC-批发-01"]["diff_cents"] == 7000
    # 旧切片重新读取：数字不变
    s1r = client.get(f"/api/asof-slices/{s1['id']}").json()
    assert {l["account_code"]: l["net_cents"] for l in s1r["lines"]} == {
        k: v["net_cents"] for k, v in l1.items()
    }


def test_slice_rows_immutable_at_db_level(client):
    """落库形态不可变：触发器拦截 UPDATE/DELETE，切片只能以新 id 追加。"""
    s = client.post("/api/asof-slices", params={"cutoff_at": "2027-06-01T00:00:00Z"}).json()
    conn = _db()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE asof_slice_lines SET net_cents = 0 WHERE slice_id = ?", (s["id"],))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE asof_slices SET net_cents = 0 WHERE id = ?", (s["id"],))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM asof_slice_lines WHERE slice_id = ?", (s["id"],))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM asof_slices WHERE id = ?", (s["id"],))
    conn.rollback()
    conn.close()
    again = client.get(f"/api/asof-slices/{s['id']}").json()
    assert again["net_cents"] == s["net_cents"]


def test_slice_generation_failure_closed(client, monkeypatch):
    """失败闭：明细写入中途失败 → 整体回滚，头与明细都不留半套。"""
    before = client.get("/api/asof-slices").json()

    def boom(conn, slice_id, lines):
        raise RuntimeError("模拟明细写入中途失败")

    monkeypatch.setattr(asof_svc, "_insert_lines", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/asof-slices", params={"cutoff_at": "2027-03-01T00:00:00Z"})

    after = client.get("/api/asof-slices").json()
    assert [s["id"] for s in after] == [s["id"] for s in before]
    conn = _db()
    orphan = conn.execute(
        "SELECT COUNT(*) AS c FROM asof_slice_lines WHERE slice_id NOT IN (SELECT id FROM asof_slices)"
    ).fetchone()["c"]
    conn.close()
    assert orphan == 0


def test_slice_bad_cutoff_rejected(client):
    """缺参/非法格式 → 400，且不落任何切片。"""
    before = client.get("/api/asof-slices").json()
    r1 = client.post("/api/asof-slices", params={"cutoff_at": "not-a-time"})
    assert r1.status_code == 400
    r2 = client.post("/api/asof-slices")
    assert r2.status_code == 400
    assert client.get("/api/asof-slices").json() == before
