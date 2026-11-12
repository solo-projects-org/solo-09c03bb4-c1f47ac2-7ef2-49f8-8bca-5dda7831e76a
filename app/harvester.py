"""采集编排：保证“翻页失败不让半批记录入库”。

流程：
1. 首次采集由调用方提供起始日；之后起始日取 harvest_progress.last_until
   （上次成功采集的截止日，含当天），实现无缝续采。
2. ListRecords 按日期拉取 oai_dc，续页仅用 resumptionToken。
3. 逐页核对：每个成功页的 <request> 回显参数须与实际发出的请求一致；
   有 cursor 时须等于本轮此前已收记录数（删除标记计入）。回显或位置
   对不上即判串页（源站缓存张冠李戴）。completeListSize 不作采完依据。
4. 串页或 resumptionToken 失效（badResumptionToken）时丢弃本轮已收页，
   按原区间整体重抓；两种故障合计只重抓一次，重抓仍逐页核对。
5. 每轮只有首请求返回 noRecordsMatch 才按空批推进截止日；续页返回该
   错误则整批失败。
6. 所有页取完后，在单个 SQLite 事务内 upsert/撤下书目并推进截止日；
   事务失败则整批回滚，目录与进度不变。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from . import db
from .oai_client import OAIClient, OAIError, OAIPage, OAIRecord

logger = logging.getLogger("harvester")

# 进程内互斥：同一时刻只允许一个采集任务，避免进度竞争
_HARVEST_LOCK = threading.Lock()

# 串页与令牌失效合计允许的重抓次数
_MAX_RETRIES = 1


class HarvestConflictError(RuntimeError):
    pass


class PageMixupError(OAIError):
    """串页：响应的 <request> 回显或 cursor 位置与本页实际请求不符。"""

    def __init__(self, message: str) -> None:
        super().__init__("pageMixup", message)


@dataclass
class HarvestSummary:
    from_date: str
    until_date: str
    pages: int
    fetched: int
    inserted: int
    updated: int
    withdrawn: int
    retries: int
    run_id: int
    status: str = "success"
    note: str | None = None


def _verify_page(page: OAIPage, *, sent_params: dict, expected_cursor: int,
                 page_no: int) -> None:
    """核对一个成功页，防源站缓存串页。

    - <request> 回显参数须与本页实际发出的请求参数完全一致；
    - 响应带 cursor 时，须等于本轮此前已收记录数（删除标记也计入）。
    """
    echo = page.request_params
    if echo != sent_params:
        raise PageMixupError(
            f"第 {page_no} 页 <request> 回显与实际请求不符："
            f"发出 {sent_params}，回显 {echo}"
        )
    if page.cursor is not None and page.cursor != expected_cursor:
        raise PageMixupError(
            f"第 {page_no} 页 cursor={page.cursor}，但本轮此前已收 "
            f"{expected_cursor} 条记录（含删除标记），位置不符"
        )


def _fetch_round(client: OAIClient, from_date: str | None,
                 until_date: str) -> tuple[list[OAIRecord], int]:
    """抓完一轮（一个日期区间的全部页），逐页核对。返回 (全部记录, 页数)。"""
    collected: list[OAIRecord] = []
    pages = 0

    sent = {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
    if from_date:
        sent["from"] = from_date
    sent["until"] = until_date
    page = client.list_records_first(from_date=from_date, until_date=until_date)
    pages += 1
    _verify_page(page, sent_params=sent, expected_cursor=0, page_no=pages)
    collected.extend(page.records)
    token = page.resumption_token

    while token:
        sent = {"verb": "ListRecords", "resumptionToken": token}
        try:
            page = client.list_records_resume(token)
        except OAIError as exc:
            if exc.code == "noRecordsMatch":
                # 续页不得返回空批：已收页面作废，整批失败（不重抓）
                raise OAIError(
                    "noRecordsMatchMidList",
                    f"第 {pages + 1} 页（续页）返回 noRecordsMatch，"
                    "本轮已收页面作废，整批失败",
                ) from exc
            raise
        pages += 1
        _verify_page(page, sent_params=sent,
                     expected_cursor=len(collected), page_no=pages)
        collected.extend(page.records)
        token = page.resumption_token
    return collected, pages


def _fetch_full_window(client: OAIClient, from_date: str | None,
                       until_date: str) -> tuple[list[OAIRecord], int, int]:
    """完整拉取一个日期区间。

    返回 (全部记录, 页数, 重抓次数)。串页或 badResumptionToken 时丢弃本轮
    已收页面、按原区间重抓；两种故障合计只重抓一次，重抓仍逐页核对。
    """
    retries = 0
    while True:
        try:
            records, pages = _fetch_round(client, from_date, until_date)
            return records, pages, retries
        except OAIError as exc:
            retryable = (
                isinstance(exc, PageMixupError)
                or exc.code == "badResumptionToken"
            )
            if not retryable:
                raise
            if retries >= _MAX_RETRIES:
                logger.error(
                    "重抓后仍发生故障（%s），放弃本批：%s", exc.code, exc.message
                )
                raise
            retries += 1
            logger.warning(
                "采集故障（%s: %s），丢弃本轮已收页面，按原区间 %s~%s 重抓"
                "（第 %d/%d 次）",
                exc.code, exc.message, from_date, until_date,
                retries, _MAX_RETRIES,
            )


def harvest(client: OAIClient, *, until_date: str,
            from_date: str | None) -> HarvestSummary:
    """执行一次采集。from_date 为 None 时从上次成功截止日续采。"""
    if not _HARVEST_LOCK.acquire(blocking=False):
        raise HarvestConflictError("已有采集任务正在执行，请稍后再试")
    try:
        return _harvest_locked(client, until_date=until_date,
                               from_date=from_date)
    finally:
        _HARVEST_LOCK.release()


def _harvest_locked(client: OAIClient, *, until_date: str,
                    from_date: str | None) -> HarvestSummary:
    conn = db.get_connection()
    run_id: int | None = None
    try:
        # 起始日：显式传入优先，否则取上次成功采集的截止日
        effective_from = from_date or db.get_last_until(conn)
        run_id = db.start_run(conn, effective_from or "", until_date)
        conn.commit()

        try:
            records, pages, retries = _fetch_full_window(
                client, effective_from, until_date
            )
        except OAIError as exc:
            if exc.code == "noRecordsMatch":
                # 无匹配：不写书目，但仍推进截止日
                return _commit_empty_window(
                    conn, run_id, effective_from, until_date
                )
            raise

        # 整批落库：单事务，任何异常整体回滚，半批数据不会入库
        with db.transaction(conn):
            inserted = updated = withdrawn = 0
            for record in records:
                if record.deleted:
                    # 遇删除标记就撤下；库中不存在则落 withdrawn 墓碑
                    if db.withdraw_bib_record(
                        conn, identifier=record.identifier,
                        datestamp=record.datestamp,
                    ):
                        withdrawn += 1
                    else:
                        db.insert_withdrawn_tombstone(
                            conn, identifier=record.identifier,
                            datestamp=record.datestamp,
                        )
                        withdrawn += 1
                    continue
                outcome = db.upsert_bib_record(
                    conn,
                    identifier=record.identifier,
                    datestamp=record.datestamp,
                    fields=_first_values(record.dc),
                    raw_dc=json.dumps(record.dc, ensure_ascii=False),
                )
                if outcome == "inserted":
                    inserted += 1
                else:
                    updated += 1

            db.advance_progress(conn, until_date)
            db.finish_run(
                conn, run_id, status="success", pages=pages,
                fetched=len(records), inserted=inserted, updated=updated,
                withdrawn=withdrawn, retries=retries, error=None,
            )
            conn.commit()

        logger.info(
            "采集成功 区间=%s~%s 页=%d 取=%d 增=%d 改=%d 撤=%d 重抓=%d",
            effective_from, until_date, pages, len(records),
            inserted, updated, withdrawn, retries,
        )
        return HarvestSummary(
            from_date=effective_from or "",
            until_date=until_date,
            pages=pages,
            fetched=len(records),
            inserted=inserted,
            updated=updated,
            withdrawn=withdrawn,
            retries=retries,
            run_id=run_id,
        )
    except Exception as exc:
        # 失败：确保事务回滚，截止日不推进；另起事务记录失败历史
        conn.rollback()
        logger.exception("采集失败，已回滚本批全部变更：%s", exc)
        if run_id is not None:
            try:
                fail_conn = db.get_connection()
                try:
                    db.finish_run(
                        fail_conn, run_id, status="failed",
                        pages=0, fetched=0, inserted=0, updated=0, withdrawn=0,
                        retries=0, error=str(exc),
                    )
                    fail_conn.commit()
                finally:
                    fail_conn.close()
            except Exception:  # pragma: no cover - 历史记录失败不掩盖主错误
                logger.exception("写入失败运行历史时出错")
        raise
    finally:
        conn.close()


def _commit_empty_window(conn, run_id: int, from_date: str | None,
                         until_date: str) -> HarvestSummary:
    with db.transaction(conn):
        db.advance_progress(conn, until_date)
        db.finish_run(
            conn, run_id, status="success", pages=0, fetched=0,
            inserted=0, updated=0, withdrawn=0, retries=0, error=None,
        )
        conn.commit()
    logger.info("区间 %s~%s 无匹配记录，截止日仍推进至 %s",
                from_date, until_date, until_date)
    return HarvestSummary(
        from_date=from_date or "",
        until_date=until_date,
        pages=0,
        fetched=0,
        inserted=0,
        updated=0,
        withdrawn=0,
        retries=0,
        run_id=run_id,
        note="区间内无匹配记录，截止日已推进",
    )


def _first_values(dc: dict) -> dict:
    """Dublin Core 多值字段取首值落列，完整多值保留在 raw_dc。"""
    return {key: (values[0] if values else None) for key, values in dc.items()}
