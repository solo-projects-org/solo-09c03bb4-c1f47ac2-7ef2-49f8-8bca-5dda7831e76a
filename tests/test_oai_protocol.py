"""OAI-PMH 客户端与样例源协议层面的单元测试。"""
from __future__ import annotations

import pytest

from app.oai_client import OAIClient, OAIError


def test_resume_request_carries_only_token(monkeypatch):
    calls = []

    class FakeTransport:
        def get(self, url, params):
            calls.append(dict(params))

            class Resp:
                status_code = 200

                def raise_for_status(self):
                    pass

                content = (
                    b'<?xml version="1.0"?>'
                    b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                    b"<responseDate>x</responseDate><request>/oai</request>"
                    b"<ListRecords>"
                    b'<resumptionToken completeListSize="2" cursor="10"/>'
                    b"</ListRecords></OAI-PMH>"
                )

            return Resp()

    client = OAIClient("http://example.test/oai")
    client._client = FakeTransport()

    client.list_records_first(from_date="2026-10-01", until_date="2026-10-15")
    client.list_records_resume("tok-123")

    assert calls[0] == {
        "verb": "ListRecords",
        "metadataPrefix": "oai_dc",
        "from": "2026-10-01",
        "until": "2026-10-15",
    }
    # 续页：resumptionToken 替代日期与格式参数
    assert calls[1] == {"verb": "ListRecords", "resumptionToken": "tok-123"}


def test_oai_error_code_surfaced(monkeypatch):
    class FakeTransport:
        def get(self, url, params):
            class Resp:
                status_code = 200

                def raise_for_status(self):
                    pass

                content = (
                    b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                    b"<responseDate>x</responseDate><request>/oai</request>"
                    b'<error code="badResumptionToken">bad token</error>'
                    b"</OAI-PMH>"
                )

            return Resp()

    client = OAIClient("http://example.test/oai")
    client._client = FakeTransport()
    with pytest.raises(OAIError) as exc:
        client.list_records_resume("nope")
    assert exc.value.code == "badResumptionToken"


def test_sample_source_rejects_token_plus_metadata_prefix(client):
    _api, _db, oai_mod = client
    from urllib.parse import parse_qs
    import xml.etree.ElementTree as ET

    qs = (
        "verb=ListRecords&resumptionToken=anything&metadataPrefix=oai_dc"
    )
    body = oai_mod._list_records(parse_qs(qs))
    ns = "{http://www.openarchives.org/OAI/2.0/}"
    error = ET.fromstring(body).find(f"{ns}error")
    assert error is not None
    assert error.get("code") == "badArgument"


def test_sample_source_mix_next_returns_foreign_last_page(client):
    """故障注入：续页请求收到“别的请求”的末页（回显与位置都对不上）。"""
    _api, _db, oai_mod = client
    from urllib.parse import parse_qs
    import xml.etree.ElementTree as ET

    ns = "{http://www.openarchives.org/OAI/2.0/}"
    oai_mod.TOKENS.set_mix_next(1)

    body = oai_mod._list_records(parse_qs(
        "verb=ListRecords&metadataPrefix=oai_dc"
        "&from=2026-10-01&until=2026-10-27"
    ))
    token = ET.fromstring(body).find(
        f"{ns}ListRecords/{ns}resumptionToken"
    ).text

    mixed = ET.fromstring(
        oai_mod._list_records(parse_qs(f"verb=ListRecords&resumptionToken={token}"))
    )
    request = mixed.find(f"{ns}request")
    # 回显的是别的请求的令牌，不是本次发出的令牌
    assert request.get("resumptionToken") != token
    rt = mixed.find(f"{ns}ListRecords/{ns}resumptionToken")
    # 末页特征：空令牌——不逐页核对的采集端会误以为采完而少采
    assert (rt.text or "").strip() == ""
    # 别的列表的末页位置，与本批已收的 10 条对不上
    assert int(rt.get("cursor")) != 10
    records = mixed.findall(f"{ns}ListRecords/{ns}record")
    assert 0 < len(records) < 27


def test_dc_fields_parsed_into_multivalue_dict(client):
    api, _db, _oai = client
    api.post("/api/v1/harvest",
             json={"from": "2026-10-01", "until": "2026-10-02"})
    record = api.get(
        "/api/v1/catalog/oai:sample.lib:rec-001"
    ).json()
    assert record["title"].startswith("样例书目 001")
    assert record["identifier_dc"] == "ISBN 978-7-0000-0001-X"
    assert record["status"] == "active"
