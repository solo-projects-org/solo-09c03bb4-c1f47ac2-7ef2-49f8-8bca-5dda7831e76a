#!/usr/bin/env python3
"""本地分页样例 OAI-PMH 2.0 源（仅使用 Python 标准库）。

提供：
  GET/POST /oai                      OAI-PMH 接口（ListRecords 支持日期 + resumptionToken 分页）
  GET      /admin/state              查看令牌库与故障开关状态
  POST     /admin/fail-next          为接下来的续页请求注入故障
                                     ?mode=bad-token（默认）|cross-page|cursor-mismatch|no-records-match
                                     ?times=N 连续影响 N 次续页请求（默认 1）
  POST     /admin/cross-page         /admin/fail-next?mode=cross-page 的简写
  POST     /admin/reset              清除故障开关与过期令牌

故障模式：
  bad-token          下一次续页请求返回 badResumptionToken（令牌失效）
  cross-page         下一次续页请求返回“别的请求的末页”：
                     <request> 回显的是另一个 resumptionToken，cursor 也对不上
  cursor-mismatch    回显与实际请求一致，但返回别的请求的末页（cursor 对不上）
  no-records-match   下一次续页请求返回 noRecordsMatch

环境变量：
  PAGE_SIZE   每页记录数，默认 10
  PORT        监听端口，默认 8080
  TOKEN_TTL   resumptionToken 有效期秒数，默认 3600
"""
from __future__ import annotations

import os
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NS = "http://purl.org/dc/elements/1.1/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
SCHEMA_LOCATION = (
    "http://www.openarchives.org/OAI/2.0/ "
    "http://www.openarchives.org/OAI/2.0/OAI-PMH.xsd"
)

ET.register_namespace("", OAI_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("oai_dc", OAI_DC_NS)
ET.register_namespace("xsi", XSI_NS)

PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "10"))
PORT = int(os.environ.get("PORT", "8080"))
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "3600"))
BASE_URL = os.environ.get("BASE_URL", f"http://oai-source:{PORT}/oai")

FAULT_MODES = {"bad-token", "cross-page", "cursor-mismatch", "no-records-match"}


def _build_records() -> list[dict]:
    """生成 27 条确定性的样例书目，其中 rec-013 为已删除记录。"""
    records = []
    start = date(2026, 10, 1)
    for i in range(1, 28):
        datestamp = (start + timedelta(days=i - 1)).isoformat()
        records.append(
            {
                "identifier": f"oai:sample.lib:rec-{i:03d}",
                "datestamp": datestamp,
                "deleted": i == 13,
                "title": f"样例书目 {i:03d}：联合目录补采测试记录",
                "creator": f"测试作者 {i:03d}",
                "date": datestamp,
                "dc_identifier": f"ISBN 978-7-0000-{i:04d}-X",
                "type": "text",
            }
        )
    return records


RECORDS = _build_records()


class TokenStore:
    """resumptionToken 的内存存储，带 TTL 与故障注入开关。

    故障开关按“接下来 N 次续页请求”计数，便于演示串页后重抓成功，
    以及重抓期间再次串页导致整批失败。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, dict] = {}
        self._fail_queue: list[str] = []

    def create(self, *, ids: list[str], cursor: int, from_date: str | None,
               until_date: str | None) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._tokens[token] = {
                "ids": ids,
                "cursor": cursor,
                "from": from_date,
                "until": until_date,
                "created_at": datetime.now(timezone.utc).timestamp(),
            }
        return token

    def consume(self, token: str) -> dict | None:
        """取出令牌；不存在或过期返回 None（模拟令牌失效）。"""
        with self._lock:
            state = self._tokens.get(token)
            if state is None:
                return None
            expired = (
                datetime.now(timezone.utc).timestamp() - state["created_at"]
                > TOKEN_TTL
            )
            if expired:
                self._tokens.pop(token, None)
                return None
            return dict(state)

    def destroy(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def take_fault(self) -> str | None:
        """若有待注入的续页故障，消耗一个并返回故障模式名。"""
        with self._lock:
            if not self._fail_queue:
                return None
            return self._fail_queue.pop(0)

    @property
    def fail_next_token(self) -> bool:
        with self._lock:
            return bool(self._fail_queue)

    def set_fail_next(self, mode: str = "bad-token", times: int = 1) -> None:
        """排队故障：mode 可用逗号分隔形成序列，如 "cross-page,bad-token"。"""
        modes = [m.strip() for m in mode.split(",") if m.strip()]
        if not modes:
            modes = ["bad-token"]
        with self._lock:
            for index in range(max(1, times)):
                self._fail_queue.append(modes[index % len(modes)])

    def reset(self) -> None:
        with self._lock:
            self._fail_queue.clear()
            self._tokens.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "page_size": PAGE_SIZE,
                "record_count": len(RECORDS),
                "active_tokens": len(self._tokens),
                "fail_next_token": bool(self._fail_queue),
                "fault_queue": list(self._fail_queue),
            }


TOKENS = TokenStore()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(params: dict | None = None) -> ET.Element:
    root = ET.Element(
        f"{{{OAI_NS}}}OAI-PMH",
        {f"{{{XSI_NS}}}schemaLocation": SCHEMA_LOCATION},
    )
    ET.SubElement(root, f"{{{OAI_NS}}}responseDate").text = _now_iso()
    request_el = ET.SubElement(root, f"{{{OAI_NS}}}request")
    request_el.text = BASE_URL
    if params:
        for key, value in params.items():
            if value is not None:
                request_el.set(key, value)
    return root


def _error_response(params: dict, code: str, message: str) -> bytes:
    root = _envelope(params)
    err = ET.SubElement(root, f"{{{OAI_NS}}}error", {"code": code})
    err.text = message
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _record_element(parent: ET.Element, record: dict) -> None:
    rec_el = ET.SubElement(parent, f"{{{OAI_NS}}}record")
    header_attrs = {"status": "deleted"} if record["deleted"] else {}
    header = ET.SubElement(rec_el, f"{{{OAI_NS}}}header", header_attrs)
    ET.SubElement(header, f"{{{OAI_NS}}}identifier").text = record["identifier"]
    ET.SubElement(header, f"{{{OAI_NS}}}datestamp").text = record["datestamp"]
    if record["deleted"]:
        return
    metadata = ET.SubElement(rec_el, f"{{{OAI_NS}}}metadata")
    dc_el = ET.SubElement(
        metadata,
        f"{{{OAI_DC_NS}}}dc",
        {
            f"{{{XSI_NS}}}schemaLocation": (
                f"{OAI_DC_NS} http://www.openarchives.org/OAI/2.0/oai_dc.xsd"
            )
        },
    )
    for tag, value in (
        ("title", record["title"]),
        ("creator", record["creator"]),
        ("date", record["date"]),
        ("identifier", record["dc_identifier"]),
        ("type", record["type"]),
    ):
        ET.SubElement(dc_el, f"{{{DC_NS}}}{tag}").text = value


def _filter_records(from_date: str | None, until_date: str | None) -> list[dict]:
    result = []
    for record in RECORDS:
        if from_date and record["datestamp"] < from_date:
            continue
        if until_date and record["datestamp"] > until_date:
            continue
        result.append(record)
    return result


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    datetime.strptime(value, "%Y-%m-%d")  # 抛 ValueError 由上层转 badArgument
    return value


def _list_records(qs: dict) -> bytes:
    token_value = qs.get("resumptionToken", [None])[0]
    fault = None
    if token_value is not None:
        # 续页请求：只允许 resumptionToken，日期/格式参数必须由令牌替代
        forbidden = [k for k in ("from", "until", "metadataPrefix", "set")
                     if k in qs]
        if forbidden:
            return _error_response(
                {"verb": "ListRecords"},
                "badArgument",
                "resumptionToken 与 from/until/set/metadataPrefix 不能同时出现",
            )
        fault = TOKENS.take_fault()
        if fault == "bad-token":
            TOKENS.destroy(token_value)
            return _error_response(
                {"verb": "ListRecords", "resumptionToken": token_value},
                "badResumptionToken",
                "resumptionToken 已失效或不存在",
            )
        if fault == "no-records-match":
            return _error_response(
                {"verb": "ListRecords", "resumptionToken": token_value},
                "noRecordsMatch",
                "续页请求（模拟）返回 noRecordsMatch",
            )
        state = TOKENS.consume(token_value)
        if state is None:
            return _error_response(
                {"verb": "ListRecords"},
                "badResumptionToken",
                "resumptionToken 已失效或不存在",
            )
        ids = state["ids"]
        cursor = state["cursor"]
        from_date, until_date = state["from"], state["until"]
        echo = {"verb": "ListRecords", "resumptionToken": token_value}
        if fault == "cross-page":
            # 模拟“缓存把别的请求的末页返回给本次续页”：
            # 回显另一个令牌，内容也取自另一个请求（全量末页，cursor 对不上）
            other_ids = [r["identifier"] for r in RECORDS]
            ids = other_ids
            cursor = len(other_ids) - min(PAGE_SIZE, len(other_ids))
            echo = {"verb": "ListRecords", "resumptionToken": "faulty-cross-page"}
        elif fault == "cursor-mismatch":
            # 回显与实际请求一致，但分页位置落在别的请求上：
            # 取另一个请求的末页位置；若恰好与本页位置相同则改用 0，
            # 保证注入的故障一定能被“cursor 对不上”识别出来
            other_cursor = len(ids) - min(PAGE_SIZE, len(ids))
            cursor = 0 if other_cursor == cursor else other_cursor
    else:
        prefix = qs.get("metadataPrefix", [None])[0]
        if prefix != "oai_dc":
            return _error_response(
                {"verb": "ListRecords"},
                "cannotDisseminateFormat",
                f"不支持的元数据格式：{prefix!r}，本样例仅支持 oai_dc",
            )
        try:
            from_date = _parse_date(qs.get("from", [None])[0])
            until_date = _parse_date(qs.get("until", [None])[0])
        except ValueError:
            return _error_response(
                {"verb": "ListRecords"}, "badArgument", "日期参数必须为 YYYY-MM-DD"
            )
        matched = _filter_records(from_date, until_date)
        if not matched:
            return _error_response(
                {"verb": "ListRecords"}, "noRecordsMatch", "区间内没有匹配记录"
            )
        ids = [r["identifier"] for r in matched]
        cursor = 0
        echo = {
            "verb": "ListRecords",
            "metadataPrefix": "oai_dc",
            "from": from_date,
            "until": until_date,
        }

    by_id = {r["identifier"]: r for r in RECORDS}
    page_ids = ids[cursor:cursor + PAGE_SIZE]
    complete = len(ids)
    next_cursor = cursor + len(page_ids)

    root = _envelope(echo)
    list_el = ET.SubElement(root, f"{{{OAI_NS}}}ListRecords")
    for identifier in page_ids:
        _record_element(list_el, by_id[identifier])

    rt_attrs = {"completeListSize": str(complete), "cursor": str(cursor)}
    rt_el = ET.SubElement(
        list_el, f"{{{OAI_NS}}}resumptionToken", rt_attrs
    )
    if next_cursor < complete:
        rt_el.text = TOKENS.create(
            ids=ids, cursor=next_cursor,
            from_date=from_date, until_date=until_date,
        )
    # 末页输出空 resumptionToken（OAI-PMH 允许），采集端据此结束翻页
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _list_identifiers(qs: dict) -> bytes:
    prefix = qs.get("metadataPrefix", [None])[0]
    if prefix != "oai_dc":
        return _error_response(
            {"verb": "ListIdentifiers"}, "cannotDisseminateFormat", "仅支持 oai_dc"
        )
    try:
        matched = _filter_records(
            _parse_date(qs.get("from", [None])[0]),
            _parse_date(qs.get("until", [None])[0]),
        )
    except ValueError:
        return _error_response(
            {"verb": "ListIdentifiers"}, "badArgument", "日期参数必须为 YYYY-MM-DD"
        )
    if not matched:
        return _error_response(
            {"verb": "ListIdentifiers"}, "noRecordsMatch", "区间内没有匹配记录"
        )
    root = _envelope({"verb": "ListIdentifiers", "metadataPrefix": "oai_dc"})
    list_el = ET.SubElement(root, f"{{{OAI_NS}}}ListIdentifiers")
    for record in matched:
        attrs = {"status": "deleted"} if record["deleted"] else {}
        header = ET.SubElement(list_el, f"{{{OAI_NS}}}header", attrs)
        ET.SubElement(header, f"{{{OAI_NS}}}identifier").text = record["identifier"]
        ET.SubElement(header, f"{{{OAI_NS}}}datestamp").text = record["datestamp"]
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _identify() -> bytes:
    root = _envelope({"verb": "Identify"})
    identify = ET.SubElement(root, f"{{{OAI_NS}}}Identify")
    values = {
        "repositoryName": "本地分页样例 OAI-PMH 源",
        "baseURL": BASE_URL,
        "protocolVersion": "2.0",
        "adminEmail": "admin@example.test",
        "earliestDatestamp": "2026-10-01",
        "deletedRecord": "transient",
        "granularity": "YYYY-MM-DD",
    }
    for tag, value in values.items():
        ET.SubElement(identify, f"{{{OAI_NS}}}{tag}").text = value
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _list_metadata_formats() -> bytes:
    root = _envelope({"verb": "ListMetadataFormats"})
    container = ET.SubElement(root, f"{{{OAI_NS}}}ListMetadataFormats")
    fmt = ET.SubElement(container, f"{{{OAI_NS}}}metadataFormat")
    ET.SubElement(fmt, f"{{{OAI_NS}}}metadataPrefix").text = "oai_dc"
    ET.SubElement(fmt, f"{{{OAI_NS}}}schema").text = (
        "http://www.openarchives.org/OAI/2.0/oai_dc.xsd"
    )
    ET.SubElement(fmt, f"{{{OAI_NS}}}metadataNamespace").text = OAI_DC_NS
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _get_record(qs: dict) -> bytes:
    identifier = qs.get("identifier", [None])[0]
    record = next((r for r in RECORDS if r["identifier"] == identifier), None)
    if record is None or qs.get("metadataPrefix", [None])[0] != "oai_dc":
        return _error_response(
            {"verb": "GetRecord"}, "idDoesNotExist", f"记录不存在：{identifier}"
        )
    root = _envelope({"verb": "GetRecord", "metadataPrefix": "oai_dc"})
    get_el = ET.SubElement(root, f"{{{OAI_NS}}}GetRecord")
    _record_element(get_el, record)
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "SampleOAI/1.0"

    def _send_xml(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/xml; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        import json

        body = json.dumps(payload, ensure_ascii=False).encode("UTF-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("UTF-8") if length else ""
            # 管理端点支持把故障参数放在 URL 查询串或表单体里
            for key, values in parse_qs(raw, keep_blank_values=True).items():
                qs.setdefault(key, values)
        return qs, parsed.path

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            qs, path = self._query()
        except Exception as exc:  # pragma: no cover - 防御性
            self._send_xml(
                _error_response({}, "badVerb", f"请求解析失败：{exc}"), 400
            )
            return

        if path == "/health":
            self._send_json({"status": "ok"})
            return
        if path == "/admin/state":
            self._send_json(TOKENS.snapshot())
            return
        if path == "/admin/fail-next":
            mode = (qs.get("mode", [None])[0] or "bad-token").strip()
            modes = [m.strip() for m in mode.split(",") if m.strip()]
            if not modes or any(m not in FAULT_MODES for m in modes):
                self._send_json(
                    {"ok": False, "message": f"未知故障模式：{mode}",
                     "supported": sorted(FAULT_MODES)},
                    400,
                )
                return
            try:
                times = int(qs.get("times", [None])[0] or 1)
            except ValueError:
                self._send_json(
                    {"ok": False, "message": "times 必须是整数"}, 400
                )
                return
            TOKENS.set_fail_next(mode=mode, times=times)
            self._send_json(
                {"ok": True, "mode": mode, "times": max(1, times),
                 "message": f"接下来 {max(1, times)} 次续页请求将按序注入故障"}
            )
            return
        if path == "/admin/cross-page":
            mode = (qs.get("mode", [None])[0] or "cross-page").strip()
            if mode not in ("cross-page", "cursor-mismatch"):
                mode = "cross-page"
            try:
                times = int(qs.get("times", [None])[0] or 1)
            except ValueError:
                times = 1
            TOKENS.set_fail_next(mode=mode, times=times)
            self._send_json(
                {"ok": True, "mode": mode, "times": max(1, times),
                 "message": f"接下来 {max(1, times)} 次续页请求将返回串页"}
            )
            return
        if path == "/admin/reset":
            TOKENS.reset()
            self._send_json({"ok": True, "message": "令牌库与故障开关已重置"})
            return
        if path != "/oai":
            self._send_xml(_error_response({}, "badVerb", f"未知路径：{path}"), 404)
            return

        verb = qs.get("verb", [None])[0]
        try:
            if verb == "Identify":
                body = _identify()
            elif verb == "ListMetadataFormats":
                body = _list_metadata_formats()
            elif verb == "ListRecords":
                body = _list_records(qs)
            elif verb == "ListIdentifiers":
                body = _list_identifiers(qs)
            elif verb == "GetRecord":
                body = _get_record(qs)
            else:
                body = _error_response({}, "badVerb", f"不支持的 verb：{verb!r}")
        except Exception as exc:  # pragma: no cover - 防御性
            body = _error_response(
                {"verb": verb} if verb else {}, "badArgument", str(exc)
            )
        self._send_xml(body)

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        print(f"[oai-source] {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[oai-source] sample OAI-PMH server listening on 0.0.0.0:{PORT}")
    print(f"[oai-source] page size={PAGE_SIZE}, records={len(RECORDS)}")
    server.serve_forever()
