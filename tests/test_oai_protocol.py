"""OAI-PMH 客户端与样例源协议层面的单元测试。"""
from __future__ import annotations

import pytest

from app.oai_client import OAIClient, OAIError


def test_resume_request_carries_only_token(monkeypatch):
    calls = []

    class FakeTransport:
        def get(self, url, params):
            calls.append(dict(params))
            attrs = "".join(
                f' {key}="{value}"' for key, value in params.items()
            )

            class Resp:
                status_code = 200

                def raise_for_status(self):
                    pass

                content = (
                    b'<?xml version="1.0"?>'
                    b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                    b"<responseDate>x</responseDate>"
                    + f"<request{attrs}>/oai</request>".encode()
                    + b"<ListRecords>"
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


def test_request_echo_mismatch_raises_page_mismatch():
    """成功页回显参数与实际请求不一致 -> pageMismatch（串页）。"""

    class FakeTransport:
        def get(self, url, params):
            class Resp:
                status_code = 200

                def raise_for_status(self):
                    pass

                content = (
                    b'<?xml version="1.0"?>'
                    b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                    b"<responseDate>x</responseDate>"
                    b'<request verb="ListRecords" metadataPrefix="oai_dc"'
                    b' from="2026-01-01" until="2026-01-31">/oai</request>'
                    b"<ListRecords>"
                    b'<resumptionToken cursor="0"/>'
                    b"</ListRecords></OAI-PMH>"
                )

            return Resp()

    client = OAIClient("http://example.test/oai")
    client._client = FakeTransport()
    with pytest.raises(OAIError) as exc:
        client.list_records_first(from_date="2026-10-01", until_date="2026-10-27")
    assert exc.value.code == "pageMismatch"
    assert "2026-01-01" in exc.value.message


def test_resume_echo_mismatch_raises_page_mismatch():
    """续页回显的是别的令牌 -> pageMismatch。"""

    class FakeTransport:
        def get(self, url, params):
            class Resp:
                status_code = 200

                def raise_for_status(self):
                    pass

                content = (
                    b'<?xml version="1.0"?>'
                    b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                    b"<responseDate>x</responseDate>"
                    b'<request verb="ListRecords" resumptionToken="other-token">'
                    b"</request>"
                    b"<ListRecords><resumptionToken cursor=\"10\"/>"
                    b"</ListRecords></OAI-PMH>"
                )

            return Resp()

    client = OAIClient("http://example.test/oai")
    client._client = FakeTransport()
    with pytest.raises(OAIError) as exc:
        client.list_records_resume("expected-token")
    assert exc.value.code == "pageMismatch"


def test_error_response_is_not_treated_as_page_mismatch():
    """错误响应只回显 verb，仍应识别 OAI error 码而不是回显不符。"""

    class FakeTransport:
        def get(self, url, params):
            class Resp:
                status_code = 200

                def raise_for_status(self):
                    pass

                content = (
                    b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                    b"<responseDate>x</responseDate>"
                    b'<request verb="ListRecords">/oai</request>'
                    b'<error code="badResumptionToken">bad token</error>'
                    b"</OAI-PMH>"
                )

            return Resp()

    client = OAIClient("http://example.test/oai")
    client._client = FakeTransport()
    with pytest.raises(OAIError) as exc:
        client.list_records_resume("nope")
    assert exc.value.code == "badResumptionToken"


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
