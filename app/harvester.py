"""采集编排：保证“翻页失败不让半批记录入库”。

流程：
1. 首次采集由调用方提供起始日；之后起始日取 harvest_progress.last_until
   （上次成功采集的截止日，含当天），实现无缝续采。
2. ListRecords 按日期拉取 oai_dc，续页仅用 resumptionToken。
3. 每个成功页都核对响应请求回显与实际请求一致，并核对 resumptionToken 的
   cursor 等于本轮此前收到的记录数（删除标记也计入）；回显或位置对不上即
   判为串页（pageMismatch）。
4. resumptionToken 失效（badResumptionToken）或串页（pageMismatch）时，
   丢弃本轮已取页，按原区间整体重抓；两种故障合计只重抓一次，重抓仍逐页
   核对；重抓再次失败则整批失败。
5. 只有每轮首请求返回 noRecordsMatch 才按空批推进截止日；续页（含重抓的
   首请求之外的任何请求）返回 noRecordsMatch 视为整批失败。
6. 所有页取完后，在单个 SQLite 事务内 upsert/撤下书目并推进截止日；
   事务失败则整批回滚，进度不变。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from . import db
from .oai_client import OAIClient, OAIError, OAIRecord

logger = logging.getLogger("harvester")

# 进程内互斥：同一时刻只允许一个采集任务，避免进度竞争
_HARVEST_LOCK = threading.Lock()


class HarvestConflictError(RuntimeError):
    pass


class FirstPageNoRecordsMatch(OAIError):
    """本轮首请求返回 noRecordsMatch：合法空区间，可按空批推进截止日。"""

    def __init__(self) -> None:
        super().__init__("noRecordsMatch", "首请求区间内没有匹配记录")
        self.retries = 0


class ResumePageNoRecordsMatch(OAIError):
    """续页返回 noRecordsMatch：不允许当空批，整批失败。"""

    def __init__(self) -> None:
        super().__init__(
            "noRecordsMatch",
            "续页请求返回 noRecordsMatch（非首请求），整批失败",
        )


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


def _fetch_full_window(client: OAIClient, from_date: str | None,
                       until_date: str) -> tuple[list[OAIRecord], int, int]:
    """完整拉取一个日期区间。

    返回 (全部记录, 页数, 因串页/令牌失效触发的重抓次数)。
    串页（pageMismatch）或续页令牌失效（badResumptionToken）时丢弃本轮
    已取页，按原区间重新开始；两种故障合计只重抓一次。重抓仍失败则抛出。
    """
    retries = 0

    def _pull() -> tuple[list[OAIRecord], int]:
        """拉取一轮：首请求 + 逐页续页，每页都核对回显与 cursor。"""
        collected: list[OAIRecord] = []
        pages = 0
        try:
            page = client.list_records_first(
                from_date=from_date, until_date=until_date
            )
        except OAIError as exc:
            if exc.code == "noRecordsMatch":
                raise FirstPageNoRecordsMatch() from exc
            raise
        pages += 1
        _verify_page(page, collected_count=0, is_first=True)
        collected.extend(page.records)
        token = page.resumption_token

        while token:
            # 续页请求只带令牌，替代日期与格式参数
            try:
                page = client.list_records_resume(token)
            except OAIError as exc:
                if exc.code == "noRecordsMatch":
                    # 续页返回 noRecordsMatch 不能当作空批，整批按失败处理
                    raise ResumePageNoRecordsMatch() from exc
                raise
            pages += 1
            _verify_page(page, collected_count=len(collected), is_first=False)
            collected.extend(page.records)
            token = page.resumption_token
        return collected, pages

    def _attempt(retrying: bool) -> tuple[list[OAIRecord], int]:
        nonlocal retries
        try:
            return _pull()
        except (FirstPageNoRecordsMatch, ResumePageNoRecordsMatch) as exc:
            # 两类 noRecordsMatch 都不属于可重抓的翻页故障，原样上抛：
            # 首请求由上层按空批推进；续页由上层整批失败。
            exc.retries = retries
            raise
        except OAIError as exc:
            if exc.code not in ("badResumptionToken", "pageMismatch"):
                exc.retries = retries
                raise
            if retrying or retries >= 1:
                # 重抓轮再次遇到串页/令牌失效：不可恢复，整批失败
                exc.retries = retries
                raise
            retries += 1
            logger.warning(
                "采集失败（%s：%s），丢弃本轮已取页，按原区间 %s~%s 重抓一次",
                exc.code, exc.message, from_date, until_date,
            )
            return _attempt(retrying=True)

    records, pages = _attempt(retrying=False)
    return records, pages, retries


def _verify_page(page, *, collected_count: int, is_first: bool) -> None:
    """逐页核对续页位置：cursor 必须等于本轮此前收到的记录数。

    - 本轮此前收到的记录数包含删除标记（删除记录同样是收下的记录）；
    - 首请求此前为 0，cursor 给出时必须为 0；
    - 每个成功页（含末页）只要有 cursor，就必须与已收到的记录数一致；
    - cursor 缺失时不判串页（无从核对位置），回显核对仍在客户端逐页进行；
    - completeListSize 只作参考，不用它判断是否采完，是否采完只看令牌有无。
    """
    if page.cursor is None:
        return
    if page.cursor != collected_count:
        position = "首请求" if is_first else "续页"
        raise OAIError(
            "pageMismatch",
            f"{position} cursor={page.cursor} 与本轮已收记录数 "
            f"{collected_count} 不一致（疑似串页）",
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
        except FirstPageNoRecordsMatch as exc:
            # 只有本轮首请求的 noRecordsMatch 才按空批推进截止日；
            # 续页的 noRecordsMatch（ResumePageNoRecordsMatch）不在此列，
            # 会落入下面的失败分支，整批回滚。
            return _commit_empty_window(
                conn, run_id, effective_from, until_date,
                retries=exc.retries,
            )

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
        failed_retries = getattr(exc, "retries", 0)
        if run_id is not None:
            try:
                fail_conn = db.get_connection()
                try:
                    db.finish_run(
                        fail_conn, run_id, status="failed",
                        pages=0, fetched=0, inserted=0, updated=0, withdrawn=0,
                        retries=failed_retries, error=str(exc),
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
                         until_date: str, *, retries: int = 0) -> HarvestSummary:
    with db.transaction(conn):
        db.advance_progress(conn, until_date)
        db.finish_run(
            conn, run_id, status="success", pages=0, fetched=0,
            inserted=0, updated=0, withdrawn=0, retries=retries, error=None,
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
        retries=retries,
        run_id=run_id,
        note="区间内无匹配记录，截止日已推进",
    )


def _first_values(dc: dict) -> dict:
    """Dublin Core 多值字段取首值落列，完整多值保留在 raw_dc。"""
    return {key: (values[0] if values else None) for key, values in dc.items()}
