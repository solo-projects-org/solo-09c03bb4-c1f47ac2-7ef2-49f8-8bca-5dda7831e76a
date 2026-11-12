"""端到端行为测试（FastAPI TestClient + 内存样例 OAI 源）。"""
from __future__ import annotations

import pytest

from app import harvester
from app.oai_client import OAIError, OAIPage, OAIRecord


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


def test_page_mixup_detected_and_refetched_once(client):
    api, _db, oai_mod = client
    # 源站缓存把“别的请求”的末页回给续页请求：逐页核对发现串页，
    # 丢弃本轮已收页后按原区间重抓成功，书目一条不少
    oai_mod.TOKENS.set_mix_next(1)
    result = _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    assert result["status"] == "success"
    assert result["retries"] == 1
    assert result["pages"] == 3
    assert result["fetched"] == 27
    assert result["inserted"] == 26
    assert result["withdrawn"] == 1
    assert api.get("/api/v1/progress").json()["last_until"] == "2026-10-27"
    assert len(api.get("/api/v1/catalog?status=all").json()) == 27


def test_persistent_mixup_rolls_back_everything(client):
    api, db_mod, oai_mod = client
    # 串页持续到重抓轮：合计只重抓一次，仍串页则整批失败
    oai_mod.TOKENS.set_mix_next(2)
    resp = api.post(
        "/api/v1/harvest",
        json={"from": "2026-10-01", "until": "2026-10-27"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["oai_error"] == "pageMixup"

    conn = db_mod.get_connection()
    try:
        # 半批记录不得入库，截止日不得推进
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 0
        assert db_mod.get_last_until(conn) is None
        run = conn.execute(
            "SELECT status, error FROM harvest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run["status"] == "failed"
        assert "pageMixup" in run["error"]
    finally:
        conn.close()


def test_mixup_and_token_failure_share_single_retry(client):
    api, db_mod, oai_mod = client
    # 首轮串页、重抓轮令牌失效：两种故障合计只重抓一次 -> 整批失败
    oai_mod.TOKENS.set_mix_next(1)
    oai_mod.TOKENS.set_fail_next()
    resp = api.post(
        "/api/v1/harvest",
        json={"from": "2026-10-01", "until": "2026-10-27"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["oai_error"] == "badResumptionToken"

    conn = db_mod.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 0
        assert db_mod.get_last_until(conn) is None
    finally:
        conn.close()


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


# ---------- 逐页核对（桩客户端，直接驱动 harvester） ----------


def _rec(identifier: str, deleted: bool = False) -> OAIRecord:
    return OAIRecord(
        identifier=identifier,
        datestamp="2026-10-01",
        deleted=deleted,
        dc={"title": [identifier]} if not deleted else {},
    )


def _first_echo(from_date: str, until_date: str) -> dict:
    return {
        "verb": "ListRecords",
        "metadataPrefix": "oai_dc",
        "from": from_date,
        "until": until_date,
    }


class _StubClient:
    """按脚本应答的 OAI 客户端；pages 为 (首页, [续页...])，异常直接抛出。"""

    def __init__(self, first, resumes):
        self._first = first
        self._resumes = list(resumes)
        self.first_calls = 0
        self.resume_calls = 0

    def list_records_first(self, *, from_date, until_date):
        self.first_calls += 1
        if isinstance(self._first, Exception):
            raise self._first
        return self._first

    def list_records_resume(self, token):
        self.resume_calls += 1
        item = self._resumes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_first_page_echo_mismatch_is_mixup_and_retried_once(client):
    _api, db_mod, _oai = client
    bad_first = OAIPage(
        records=[_rec("r1")], resumption_token=None, complete_list_size=1,
        cursor=0,
        # 回显缺 metadataPrefix / from，与实际请求不符 -> 串页
        request_params={"verb": "ListRecords", "until": "2026-10-27"},
    )
    stub = _StubClient(bad_first, [])
    with pytest.raises(OAIError) as exc:
        harvester.harvest(stub, until_date="2026-10-27",
                          from_date="2026-10-01")
    assert exc.value.code == "pageMixup"
    # 串页合计只重抓一次：首请求共发出 2 次
    assert stub.first_calls == 2
    conn = db_mod.get_connection()
    try:
        assert db_mod.get_last_until(conn) is None
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 0
    finally:
        conn.close()


def test_cursor_mismatch_is_mixup(client):
    _api, db_mod, _oai = client
    first = OAIPage(
        records=[_rec("r1"), _rec("r2")], resumption_token="tok",
        complete_list_size=3, cursor=0,
        request_params=_first_echo("2026-10-01", "2026-10-27"),
    )
    bad_second = OAIPage(
        records=[_rec("r3")], resumption_token=None, complete_list_size=3,
        cursor=5,  # 本轮此前已收 2 条，cursor 应为 2
        request_params={"verb": "ListRecords", "resumptionToken": "tok"},
    )
    stub = _StubClient(first, [bad_second, bad_second])
    with pytest.raises(OAIError) as exc:
        harvester.harvest(stub, until_date="2026-10-27",
                          from_date="2026-10-01")
    assert exc.value.code == "pageMixup"
    assert "cursor" in exc.value.message
    # 首轮第 2 页串页 -> 重抓一轮仍串页：续页请求共 2 次
    assert stub.resume_calls == 2
    conn = db_mod.get_connection()
    try:
        assert db_mod.get_last_until(conn) is None
    finally:
        conn.close()


def test_deleted_records_count_toward_cursor(client):
    _api, db_mod, _oai = client
    first = OAIPage(
        records=[_rec("r1"), _rec("r2", deleted=True)],  # 删除标记也占位置
        resumption_token="tok", complete_list_size=3, cursor=0,
        request_params=_first_echo("2026-10-01", "2026-10-27"),
    )
    second = OAIPage(
        records=[_rec("r3")], resumption_token=None, complete_list_size=3,
        cursor=2,  # 含删除标记在内的已收记录数
        request_params={"verb": "ListRecords", "resumptionToken": "tok"},
    )
    stub = _StubClient(first, [second])
    summary = harvester.harvest(stub, until_date="2026-10-27",
                                from_date="2026-10-01")
    assert summary.fetched == 3
    assert summary.inserted == 2
    assert summary.withdrawn == 1
    assert summary.retries == 0


def test_no_records_match_on_resume_fails_batch_without_retry(client):
    _api, db_mod, _oai = client
    first = OAIPage(
        records=[_rec("r1")], resumption_token="tok",
        complete_list_size=2, cursor=0,
        request_params=_first_echo("2026-10-01", "2026-10-27"),
    )
    stub = _StubClient(first, [OAIError("noRecordsMatch", "区间内没有匹配记录")])
    with pytest.raises(OAIError) as exc:
        harvester.harvest(stub, until_date="2026-10-27",
                          from_date="2026-10-01")
    # 续页返回 noRecordsMatch：整批失败，且不按空批推进、不重抓
    assert exc.value.code == "noRecordsMatchMidList"
    assert stub.first_calls == 1
    conn = db_mod.get_connection()
    try:
        assert db_mod.get_last_until(conn) is None
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 0
    finally:
        conn.close()
