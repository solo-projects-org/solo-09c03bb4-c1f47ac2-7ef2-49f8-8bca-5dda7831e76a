"""端到端行为测试（FastAPI TestClient + 内存样例 OAI 源）。"""
from __future__ import annotations


def _harvest(client, payload):
    resp = client.post("/api/v1/harvest", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_first_harvest_paginates_and_marks_deleted(client):
    api, _db, _oai = client
    result = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    # 15 条记录、PAGE_SIZE=10 -> 2 页；rec-013 为删除标记
    assert result["pages"] == 2
    assert result["fetched"] == 15
    assert result["inserted"] == 14
    assert result["withdrawn"] == 1
    assert result["retries"] == 0

    progress = api.get("/api/v1/progress").json()
    assert progress["last_until"] == "2026-10-15"

    catalog = api.get("/api/v1/catalog?status=all").json()
    assert len(catalog) == 15
    deleted = next(r for r in catalog if r["identifier"] == "oai:sample.lib:rec-013")
    assert deleted["status"] == "withdrawn"
    assert all(r["status"] == "active"
               for r in catalog if r["identifier"] != "oai:sample.lib:rec-013")

    # 默认只看在架
    active = api.get("/api/v1/catalog").json()
    assert len(active) == 14


def test_resume_uses_last_until_inclusive(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    # 不传 from：起始日自动取上次截止日 2026-10-15（含当天）
    result = _harvest(api, {"until": "2026-10-27"})
    assert result["from_date"] == "2026-10-15"
    # 10-15..10-27 共 13 条，其中 10-15 已在库 -> 12 新增 + 1 更新
    assert result["fetched"] == 13
    assert result["inserted"] == 12
    assert result["updated"] == 1
    assert api.get("/api/v1/progress").json()["last_until"] == "2026-10-27"


def test_idempotent_reharvest_only_updates(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    again = _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    assert again["inserted"] == 0
    assert again["updated"] == 26
    assert again["withdrawn"] == 1
    assert len(api.get("/api/v1/catalog?status=all").json()) == 27


def test_bad_resumption_token_refetches_window_once(client):
    api, _db, oai_mod = client
    # 下一次续页请求令牌失效：已取页作废，按原区间重抓一次后成功
    oai_mod.TOKENS.set_fail_next()
    result = _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    assert result["status"] == "success"
    assert result["retries"] == 1
    assert result["pages"] == 3
    assert result["fetched"] == 27
    assert result["inserted"] == 26
    assert result["withdrawn"] == 1


def test_cross_page_echo_mismatch_refetches_window_once(client):
    api, _db, oai_mod = client
    # 续页回显的是别的请求：已取页作废，按原区间重抓后成功
    oai_mod.TOKENS.set_fail_next(mode="cross-page", times=1)
    result = _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    assert result["status"] == "success"
    assert result["retries"] == 1
    assert result["pages"] == 3
    assert result["fetched"] == 27
    assert api.get("/api/v1/progress").json()["last_until"] == "2026-10-27"


def test_cursor_mismatch_with_matching_echo_refetches_window_once(client):
    api, _db, oai_mod = client
    # 回显参数与实际请求一致，但返回别的请求的末页（cursor 对不上）
    oai_mod.TOKENS.set_fail_next(mode="cursor-mismatch", times=1)
    result = _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    assert result["status"] == "success"
    assert result["retries"] == 1
    assert result["fetched"] == 27


def test_second_fault_during_refetch_fails_without_extra_retry(client, monkeypatch):
    api, db_mod, oai_mod = client
    # 两种故障合计只重抓一次：重抓期间再遇到串页，整批失败
    oai_mod.TOKENS.set_fail_next(mode="cross-page", times=2)
    resp = api.post(
        "/api/v1/harvest",
        json={"from": "2026-10-01", "until": "2026-10-27"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["oai_error"] == "pageMismatch"

    conn = db_mod.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 0
        assert db_mod.get_last_until(conn) is None
        run = conn.execute(
            "SELECT status, retries, error FROM harvest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run["status"] == "failed"
        # 重抓过一次，失败历史里也应记录本次实际重抓次数
        assert run["retries"] == 1
        assert "pageMismatch" in run["error"]
    finally:
        conn.close()


def test_resume_no_records_match_fails_whole_batch(client):
    api, db_mod, oai_mod = client
    # 续页返回 noRecordsMatch：不能按空批推进，整批失败
    oai_mod.TOKENS.set_fail_next(mode="no-records-match", times=1)
    resp = api.post(
        "/api/v1/harvest",
        json={"from": "2026-10-01", "until": "2026-10-27"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["oai_error"] == "noRecordsMatch"

    conn = db_mod.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 0
        assert db_mod.get_last_until(conn) is None
        run = conn.execute(
            "SELECT status, error FROM harvest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run["status"] == "failed"
        assert "noRecordsMatch" in run["error"]
    finally:
        conn.close()


def test_cross_page_failure_leaves_catalog_and_progress_unchanged(client):
    api, db_mod, oai_mod = client
    # 先成功采一批，再让下一次采集重抓仍串页：目录与截止日保持上次成功状态
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    before = api.get("/api/v1/catalog?status=all").json()
    before_progress = api.get("/api/v1/progress").json()

    oai_mod.TOKENS.set_fail_next(mode="cross-page", times=2)
    resp = api.post(
        "/api/v1/harvest",
        json={"from": "2026-10-01", "until": "2026-10-27"},
    )
    assert resp.status_code == 502
    assert api.get("/api/v1/catalog?status=all").json() == before
    assert api.get("/api/v1/progress").json() == before_progress


def test_persistent_token_failure_rolls_back_everything(client, monkeypatch):
    api, db_mod, oai_mod = client

    # 让令牌持续失效：重抓一次后仍失败 -> 整批回滚
    real_consume = oai_mod.TOKENS.consume

    def always_invalid(_token):
        real_consume(_token)  # 保持内部状态清理
        return None

    monkeypatch.setattr(oai_mod.TOKENS, "consume", always_invalid)

    resp = api.post(
        "/api/v1/harvest",
        json={"from": "2026-10-01", "until": "2026-10-27"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["oai_error"] == "badResumptionToken"

    conn = db_mod.get_connection()
    try:
        # 半批记录不得入库，截止日不得推进
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 0
        assert db_mod.get_last_until(conn) is None
        run = conn.execute(
            "SELECT status, error FROM harvest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run["status"] == "failed"
        assert "badResumptionToken" in run["error"]
    finally:
        conn.close()


def test_last_page_cursor_is_also_checked(client, monkeypatch):
    """末页（空令牌）也必须核对 cursor：位置对不上同样判串页。"""
    api, _db, oai_mod = client
    real_list = oai_mod._list_records
    tampered_pages = {"count": 0}

    # 让样例源在末页返回错误 cursor：第 3 页正常 cursor 应为 20
    def tamper(qs):
        body = real_list(qs)
        if qs.get("resumptionToken", [None])[0]:
            import xml.etree.ElementTree as ET
            ns = "{http://www.openarchives.org/OAI/2.0/}"
            root = ET.fromstring(body)
            list_el = root.find(f"{ns}ListRecords")
            rt = list_el.find(f"{ns}resumptionToken")
            if rt is not None and not (rt.text or "").strip():
                rt.set("cursor", "0")  # 末页被改成 cursor=0
                tampered_pages["count"] += 1
                return ET.tostring(root, encoding="UTF-8", xml_declaration=True)
        return body

    oai_mod._list_records = tamper
    try:
        resp = api.post(
            "/api/v1/harvest",
            json={"from": "2026-10-01", "until": "2026-10-27"},
        )
    finally:
        oai_mod._list_records = real_list

    # 两轮（首轮 + 一次重抓）的末页都被改坏，说明重抓期间同样逐页核对
    assert tampered_pages["count"] == 2
    assert resp.status_code == 502
    assert resp.json()["detail"]["oai_error"] == "pageMismatch"
    assert api.get("/api/v1/catalog?status=all").json() == []
    assert api.get("/api/v1/progress").json()["last_until"] is None


def test_no_match_still_advances_until(client):
    api, _db, _oai = client
    result = _harvest(api, {"from": "2027-01-01", "until": "2027-01-31"})
    assert result["fetched"] == 0
    assert result["pages"] == 0
    assert result["note"]
    assert api.get("/api/v1/progress").json()["last_until"] == "2027-01-31"
    assert api.get("/api/v1/catalog").json() == []


def test_invalid_dates_rejected(client):
    api, _db, _oai = client
    assert api.post(
        "/api/v1/harvest", json={"until": "2026-13-99"}
    ).status_code == 422
    resp = api.post(
        "/api/v1/harvest",
        json={"from": "2026-10-20", "until": "2026-10-01"},
    )
    assert resp.status_code == 422


def test_runs_history_records_every_attempt(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    _harvest(api, {"from": "2027-01-01", "until": "2027-01-31"})
    runs = api.get("/api/v1/runs").json()
    assert [r["status"] for r in runs] == ["success", "success"]
    assert runs[0]["until_date"] == "2027-01-31"
    assert runs[1]["fetched"] == 15
