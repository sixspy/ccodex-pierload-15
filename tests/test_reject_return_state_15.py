"""驳回回态第15号（回态案辰）：旁路驳回须同事务回写主票态。

正确性口径（二选一，不得混用，旁门第三写法禁止）：
- 甲路径：驳回动作同事务把主票态写成「已裁定」并清除旁路标记，否则整笔回滚拒收。
- 乙路径：驳回仅落影子回态草案，正式票态零改，待确认后晋升。

本仓取甲路径：驳回即正式回态，无影子草案表。
三红：
1. 红测甲——挂起已闭而票态仍停旁路（放行页不可用、对账却可见已裁定）。
2. 红测乙——影子回态未晋升却被对账当已裁定（本仓不得存在影子态）。
3. 争用红测——双人同票抢驳回，后到只记争用痕，业务零写。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    # 先用「驳回后票态未回写」的种子夹具：fixtures/overloads.json 中
    # WT-LN-N-01-IN-002 为开放挂起，tickets.json 中其票态为「旁路挂起」。
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _seeded_pending(c: TestClient, h: dict) -> dict:
    """种子夹具里那条开放旁路挂起票（驳回后票态未回写的现场）。"""
    tickets = c.get("/api/tickets?status=旁路挂起", headers=h).json()["items"]
    return next(t for t in tickets if t["ticket_code"] == "WT-LN-N-01-IN-002")


def _open_hold(c: TestClient, h: dict, ticket_id: int) -> dict:
    holds = c.get("/api/overloads?status=开放", headers=h).json()["items"]
    return next(x for x in holds if x["ticket_id"] == ticket_id)


# --- 红测甲：驳回须同事务把主票态回写为已裁定并清除旁路标记 -------------

def test_reject_writes_back_ticket_status_same_tx():
    c = TestClient(app)
    h = _auth(c)
    pending = _seeded_pending(c, h)
    hold = _open_hold(c, h, pending["id"])

    res = c.post(
        "/api/overloads/reject",
        headers=h,
        json={"hold_id": hold["id"], "note": "驳回回态纪要：当班封死"},
    )
    assert res.status_code == 200, res.text

    # 挂起已闭
    hold_after = c.get("/api/overloads", headers=h).json()["items"]
    closed = next(x for x in hold_after if x["id"] == hold["id"])
    assert closed["status"] == "已驳回"

    # 甲路径核心断言：主票态必须同事务回到「已裁定」，不得再停旁路挂起
    ticket = c.get(f"/api/tickets/{pending['id']}", headers=h).json()["ticket"]
    assert ticket["status"] == "已裁定"
    assert ticket["adjudicated_at"]

    # 放行页：回态后该票应可被选为可放行票（状态=已裁定且无开放挂起）
    releasable = c.get("/api/tickets?status=已裁定", headers=h).json()["items"]
    assert any(t["id"] == pending["id"] for t in releasable)
    gate = c.post(
        "/api/gate/release",
        headers=h,
        json={"ticket_id": pending["id"], "officer": "验收员"},
    )
    assert gate.status_code == 200, gate.text

    # 总览页旁路挂起计数必须归零同态
    dash = c.get("/api/dashboard", headers=h).json()
    assert dash["side_pending"] == 0
    assert dash["open_overloads"] == 0

    # 驳回回态纪要封死在审计里（当班封死，不可只改字样）
    audits = c.get("/api/audit", headers=h).json()["items"]
    rejects = [a for a in audits if a["action"] == "overload_reject"]
    assert rejects
    assert "驳回回态纪要：当班封死" in rejects[0]["payload"]["note"]


# --- 红测乙：不得有影子回态；对账只能认正式票态，未晋升不得当已裁定 -------

def test_no_shadow_return_state_reconcile_uses_official_status_only():
    c = TestClient(app)
    h = _auth(c)
    pending = _seeded_pending(c, h)
    hold = _open_hold(c, h, pending["id"])

    # 乙路径在本仓被否决：系统中不得存在任何影子/草案回态载体
    # （overload_holds 只允许 开放/已驳回 等正式态，weigh_tickets 只认正式票态）。
    res = c.post(
        "/api/overloads/reject",
        headers=h,
        json={"hold_id": hold["id"], "note": "驳回，正式回态"},
    )
    assert res.status_code == 200, res.text

    ticket = c.get(f"/api/tickets/{pending['id']}", headers=h).json()["ticket"]
    # 正式票态零改的影子写法若混入，这里会仍是旁路挂起
    assert ticket["status"] == "已裁定"

    # 对账页只按正式票态取数：该票以已裁定身份进入对账，且无任何草案态漏出
    recon = c.get(f"/api/reconcile/{pending['bl_ref']}", headers=h).json()
    row = next(t for t in recon["tickets"] if t["ticket_code"] == pending["ticket_code"])
    assert row["status"] == "已裁定"
    assert "草案" not in row["status"] and "影子" not in row["status"]

    # 证明页：超重闭环且票态正式回态后才 coherent
    proof = c.get(
        f"/api/proof/ticket/{pending['ticket_code']}", headers=h
    ).json()
    assert proof["overload_closed"] is True


# --- 争用红测：双人同票抢驳回，后到只记争用痕，业务零写 ------------------

def test_concurrent_reject_same_hold_contention_recorded():
    from db import connect, now_iso

    c = TestClient(app)
    h1 = _auth(c, "admin", "admin123")
    h2 = _auth(c, "writer", "writer123")
    pending = _seeded_pending(c, h1)
    hold = _open_hold(c, h1, pending["id"])
    lock_key = f"ticket:{pending['id']}"

    # 模拟当班已有一笔处理持有该票挂起锁：两人随后并发驳回同一票点，
    # 后到者抢不到挂起锁，只许留争用痕。
    with connect() as conn:
        conn.execute(
            "INSERT INTO pier_locks(lock_key,holder,acquired_at) VALUES(?,?,?)",
            (lock_key, "on-duty-holder", now_iso()),
        )
        conn.commit()

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def reject(headers: dict) -> None:
        cc = TestClient(app)
        barrier.wait()
        r = cc.post(
            "/api/overloads/reject",
            headers=headers,
            json={"hold_id": hold["id"], "note": "并发驳回"},
        )
        results.append({"status": r.status_code, "body": r.text})

    t1 = threading.Thread(target=reject, args=(h1,))
    t2 = threading.Thread(target=reject, args=(h2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # 两人皆抢锁失败：只记争用，不写业务
    assert sorted(r["status"] for r in results) == [409, 409], results

    # 挂起仍为开放、票态仍旁路——驳回一笔都不许落
    holds = c.get("/api/overloads", headers=h1).json()["items"]
    still_open = next(x for x in holds if x["id"] == hold["id"])
    assert still_open["status"] == "开放"
    ticket = c.get(f"/api/tickets/{pending['id']}", headers=h1).json()["ticket"]
    assert ticket["status"] == "旁路挂起"

    # 后到（两个被拒者）均只记争用痕
    contention = c.get("/api/contention", headers=h1).json()["items"]
    marks = [x for x in contention if x["lock_key"] == lock_key and x["action"] == "争用"]
    assert len(marks) == 2
    assert {m["actor"] for m in marks} == {"admin", "writer"}
