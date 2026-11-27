"""分析记录与观点写入。

负责 ``analysis_attempts``／``model_attempts`` 的登记与缓存查询，以及
``save_opinions`` 的整笔事务：核对输入版本、按业务键更新观点、替换证据、
删除当前结果已不存在的旧观点。
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any

from backend.config import Settings, get_settings
from backend.db import open_connection, transaction
from backend.errors import (
    DatabaseOperationError,
    STATUS_FAILED,
    STATUS_SAVED,
    STATUS_STALE,
)
from backend.models import AnalysisResult, SaveResult, ScopeResult
from backend.repository import path_key_of
from backend.textutil import canonical_json, sha256_hex

LOGGER = logging.getLogger(__name__)

UNKNOWN_SORT_DAY = datetime.date(1000, 1, 1)
CACHEABLE_MODEL_STATUSES = ("inferred", "abstained")


def compute_input_hash(input_parts: list[dict[str, Any]]) -> str:
    """按固定字段顺序计算本次输入指纹。

    :param input_parts: 每项含 ``role``、``relative_path``、``article_id``、
        ``content_hash``。
    :return: 64 位小写十六进制 SHA-256。
    """
    normalized = [
        {
            "role": part.get("role"),
            "relative_path": part.get("relative_path"),
            "article_id": part.get("article_id"),
            "content_hash": part.get("content_hash"),
        }
        for part in input_parts
    ]
    normalized.sort(
        key=lambda part: (
            str(part["role"]),
            str(part["relative_path"]),
            part["article_id"] or 0,
        )
    )
    return sha256_hex(canonical_json(normalized))


def compute_report_group_key(
    institution: str | None,
    published_on: datetime.date | None,
    title: str | None,
    article_id: int,
) -> bytes:
    """生成报告去重键。

    :param institution: 机构名。
    :param published_on: 发布日期。
    :param title: 报告标题。
    :param article_id: 文章 ID，字段都缺失时作为兜底。
    :return: 32 字节键。
    """
    if not institution and not title and published_on is None:
        seed = f"article:{article_id}"
    else:
        seed = canonical_json(
            {
                "institution": institution or "",
                "published_on": published_on.isoformat() if published_on else "",
                "title": title or "",
            }
        )
    return bytes.fromhex(sha256_hex(seed))


def begin_attempt(
    article_id: int,
    input_hash: str,
    analyzer_version: str,
    *,
    settings: Settings | None = None,
) -> int:
    """登记一次分析运行。

    :param article_id: 主文章 ID。
    :param input_hash: 输入指纹。
    :param analyzer_version: 规则与编排版本。
    :param settings: 运行配置。
    :return: 分析号。
    """
    with open_connection(settings) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO analysis_attempts
                    (article_id, input_hash, analyzer_version, started_at, status)
                VALUES (%s, %s, %s, NOW(), 'running')
                """,
                (article_id, bytes.fromhex(input_hash), analyzer_version),
            )
            attempt_id = int(cursor.lastrowid)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
    return attempt_id


def finish_attempt(
    attempt_id: int,
    status: str,
    *,
    error_code: str | None = None,
    result_count: int = 0,
    settings: Settings | None = None,
) -> None:
    """更新分析运行状态。

    :param attempt_id: 分析号。
    :param status: ``saved``、``failed`` 等状态。
    :param error_code: 失败原因码。
    :param result_count: 写入的观点数。
    :param settings: 运行配置。
    """
    with open_connection(settings) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE analysis_attempts
                SET status = %s, error_code = %s, result_count = %s,
                    finished_at = NOW()
                WHERE id = %s
                """,
                (status, error_code, result_count, attempt_id),
            )
            connection.commit()
        finally:
            cursor.close()


def record_model_attempt(
    attempt_id: int,
    *,
    scope_id: str,
    fragment_hash: str,
    model_id: str,
    prompt_version: str,
    status: str,
    error_code: str | None,
    validated_result: dict[str, Any] | None,
    settings: Settings | None = None,
) -> None:
    """在结果事务之外登记一次模型尝试。

    :param attempt_id: 分析号。
    :param scope_id: 范围编号。
    :param fragment_hash: 片段指纹。
    :param model_id: 模型标识。
    :param prompt_version: 提示模板版本。
    :param status: ``inferred``、``abstained``、``invalid`` 或 ``error``。
    :param error_code: 失败原因码。
    :param validated_result: 通过校验的结果 JSON。
    :param settings: 运行配置。
    """
    with open_connection(settings) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO model_attempts
                    (attempt_id, scope_id, fragment_hash, model_id,
                     prompt_version, status, error_code, validated_result)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attempt_id,
                    scope_id,
                    bytes.fromhex(fragment_hash),
                    model_id,
                    prompt_version,
                    status,
                    error_code,
                    json.dumps(validated_result, ensure_ascii=False)
                    if validated_result is not None
                    else None,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()


def find_cached_model_attempt(
    article_id: int,
    *,
    scope_id: str,
    fragment_hash: str,
    model_id: str,
    prompt_version: str,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """查找可复用的已校验模型结果。

    :param article_id: 主文章 ID。
    :param scope_id: 范围编号。
    :param fragment_hash: 片段指纹。
    :param model_id: 模型标识。
    :param prompt_version: 提示模板版本。
    :param settings: 运行配置。
    :return: ``{"status", "validated_result"}``，无缓存时为 ``None``。
    """
    with open_connection(settings, autocommit=True) as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT m.status, m.validated_result
                FROM model_attempts AS m
                JOIN analysis_attempts AS a ON a.id = m.attempt_id
                WHERE a.article_id = %s AND m.scope_id = %s
                  AND m.fragment_hash = %s AND m.model_id = %s
                  AND m.prompt_version = %s AND m.status IN (%s, %s)
                ORDER BY m.id DESC
                LIMIT 1
                """,
                (
                    article_id,
                    scope_id,
                    bytes.fromhex(fragment_hash),
                    model_id,
                    prompt_version,
                    *CACHEABLE_MODEL_STATUSES,
                ),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
    if row is None:
        return None
    raw_result = row["validated_result"]
    if isinstance(raw_result, str):
        raw_result = json.loads(raw_result)
    return {"status": row["status"], "validated_result": raw_result}


def _lock_input_versions(
    cursor: Any,
    result: AnalysisResult,
) -> bool:
    """锁定并核对本次输入的所有原文版本。

    :param cursor: 事务游标。
    :param result: 分析结果。
    :return: 输入仍然有效时为 ``True``；有版本变化或缺失附件已入库时为 ``False``。
    """
    article_ids = [
        part.article_id
        for part in result.input_parts
        if part.article_id is not None
    ]
    current: dict[int, str] = {}
    if article_ids:
        placeholders = ", ".join(["%s"] * len(article_ids))
        cursor.execute(
            f"SELECT id, LOWER(HEX(content_hash)) AS content_hash "
            f"FROM raw_articles WHERE id IN ({placeholders}) FOR UPDATE",
            tuple(article_ids),
        )
        current = {
            int(row["id"]): str(row["content_hash"]) for row in cursor.fetchall()
        }
    for part in result.input_parts:
        if part.article_id is None:
            if part.relative_path and _attachment_registered(cursor, part.relative_path):
                LOGGER.info("缺失附件已入库，结果过期：%s", part.relative_path)
                return False
            continue
        if current.get(part.article_id) != part.content_hash:
            LOGGER.info("原文版本已变化，结果过期：article_id=%s", part.article_id)
            return False
    return True


def _attachment_registered(cursor: Any, relative_path: str) -> bool:
    """检查此前缺失的附件是否已入库。

    :param cursor: 事务游标。
    :param relative_path: 附件规范相对路径。
    :return: 已登记时为 ``True``。
    """
    cursor.execute(
        "SELECT id FROM raw_articles WHERE path_key = %s FOR UPDATE",
        (path_key_of(relative_path),),
    )
    return cursor.fetchone() is not None


def _upsert_report(
    cursor: Any,
    result: AnalysisResult,
) -> int:
    """按主文章登记或更新报告行。

    :param cursor: 事务游标。
    :param result: 分析结果。
    :return: 报告 ID。
    """
    metadata = result.metadata
    institution = metadata.institution if metadata else None
    title = metadata.title if metadata else None
    published_on = metadata.published_on if metadata else None
    date_source = metadata.date_source if metadata else None
    group_key = compute_report_group_key(
        institution, published_on, title, result.article_id
    )
    input_parts_json = json.dumps(
        [part.to_dict() for part in result.input_parts], ensure_ascii=False
    )
    metadata_review = bool(metadata.issues) if metadata else False
    main_hash = next(
        (
            part.content_hash
            for part in result.input_parts
            if part.role == "main"
        ),
        None,
    )

    cursor.execute(
        """
        SELECT id, committed_attempt_id FROM reports
        WHERE raw_article_id = %s FOR UPDATE
        """,
        (result.article_id,),
    )
    existing = cursor.fetchone()
    if existing is None:
        cursor.execute(
            """
            INSERT INTO reports
                (raw_article_id, content_hash, input_hash, input_parts,
                 institution, title, published_on, date_source,
                 analyzer_version, report_group_key, metadata_review,
                 committed_attempt_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                result.article_id,
                bytes.fromhex(main_hash) if main_hash else None,
                bytes.fromhex(result.input_hash),
                input_parts_json,
                institution,
                title,
                published_on,
                date_source,
                result.analyzer_version,
                group_key,
                metadata_review,
                result.attempt_id,
            ),
        )
        return int(cursor.lastrowid)

    report_id = int(existing["id"])
    committed = existing["committed_attempt_id"]
    if (
        committed is not None
        and result.attempt_id is not None
        and int(committed) > int(result.attempt_id)
    ):
        raise StaleReportError("已有更新的一次分析提交")
    cursor.execute(
        """
        UPDATE reports
        SET content_hash = %s, input_hash = %s, input_parts = %s,
            institution = %s, title = %s, published_on = %s, date_source = %s,
            analyzer_version = %s, report_group_key = %s,
            metadata_review = %s, committed_attempt_id = %s
        WHERE id = %s
        """,
        (
            bytes.fromhex(main_hash) if main_hash else None,
            bytes.fromhex(result.input_hash),
            input_parts_json,
            institution,
            title,
            published_on,
            date_source,
            result.analyzer_version,
            group_key,
            metadata_review,
            result.attempt_id,
            report_id,
        ),
    )
    return report_id


class StaleReportError(Exception):
    """本次结果已过期，不应写入。"""


def _write_scope(
    cursor: Any,
    report_id: int,
    scope: ScopeResult,
    sort_day: datetime.date,
) -> int:
    """按业务键写入一条观点并替换其证据。

    :param cursor: 事务游标。
    :param report_id: 报告 ID。
    :param scope: 范围结果。
    :param sort_day: 排序日。
    :return: 观点 ID。
    """
    contract_key = (scope.contract or "").upper()
    conditions_json = json.dumps(scope.conditions, ensure_ascii=False)
    cursor.execute(
        """
        INSERT INTO opinions
            (report_id, product_id, contract_key, period_key, period_text,
             period_definition, direction, source, statement_type,
             review_state, reason_code, outlook_text, conditions,
             inference_reason, sort_day)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            id = LAST_INSERT_ID(id),
            period_text = VALUES(period_text),
            period_definition = VALUES(period_definition),
            direction = VALUES(direction),
            source = VALUES(source),
            statement_type = VALUES(statement_type),
            review_state = VALUES(review_state),
            reason_code = VALUES(reason_code),
            outlook_text = VALUES(outlook_text),
            conditions = VALUES(conditions),
            inference_reason = VALUES(inference_reason),
            sort_day = VALUES(sort_day)
        """,
        (
            report_id,
            scope.product_id,
            contract_key,
            scope.period_key,
            scope.period_text,
            scope.period_definition,
            scope.direction,
            scope.source,
            scope.statement_type,
            scope.review_state,
            scope.reason_code,
            scope.outlook_text,
            conditions_json,
            scope.inference_reason,
            sort_day,
        ),
    )
    opinion_id = int(cursor.lastrowid)
    cursor.execute("DELETE FROM opinion_evidence WHERE opinion_id = %s", (opinion_id,))
    for evidence in scope.evidence:
        cursor.execute(
            """
            INSERT INTO opinion_evidence
                (opinion_id, kind, rule_id, grade, quote, block_id,
                 raw_article_id, content_hash, location, attachment_chain, contract)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                opinion_id,
                evidence.kind,
                evidence.rule_id,
                evidence.grade,
                evidence.quote,
                evidence.block_id,
                evidence.raw_article_id,
                bytes.fromhex(evidence.content_hash),
                json.dumps(evidence.location, ensure_ascii=False),
                json.dumps(evidence.attachment_chain, ensure_ascii=False),
                evidence.contract,
            ),
        )
    return opinion_id


def save_opinions(
    result: AnalysisResult,
    *,
    settings: Settings | None = None,
) -> SaveResult:
    """在一个事务里写入报告、观点与证据。

    :param result: 编排结果。
    :param settings: 运行配置。
    :return: ``saved``、``stale`` 或 ``failed``。
    """
    settings = settings or get_settings()
    if not any(part.role == "main" for part in result.input_parts):
        LOGGER.error("缺少主文输入版本，拒绝写入：article_id=%s", result.article_id)
        return SaveResult(
            status=STATUS_FAILED,
            attempt_id=result.attempt_id,
            error_code="input_incomplete",
        )
    try:
        return _save_with_retry(result, settings)
    except StaleReportError as error:
        LOGGER.info("写入被判定过期：%s", error)
        return SaveResult(
            status=STATUS_STALE,
            attempt_id=result.attempt_id,
            error_code="input_changed",
        )
    except Exception as error:  # noqa: BLE001 - 对外只交付失败原因
        LOGGER.exception("写入观点失败：article_id=%s", result.article_id)
        return SaveResult(
            status=STATUS_FAILED,
            attempt_id=result.attempt_id,
            error_code=getattr(error, "code", "database_error"),
        )


def _save_with_retry(result: AnalysisResult, settings: Settings) -> SaveResult:
    """执行写入，遇到锁冲突时整笔重试。

    :param result: 编排结果。
    :param settings: 运行配置。
    :return: 写入结果。
    """
    attempts = 3
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _save_once(result, settings)
        except (StaleReportError, DatabaseOperationError):
            raise
        except Exception as error:  # noqa: BLE001 - 按错误号决定是否重试
            from backend.db import is_retryable

            last_error = error
            if not is_retryable(error) or attempt == attempts:
                raise
            LOGGER.warning("写入遇到锁冲突，第 %s 次重试：%s", attempt, error)
    raise DatabaseOperationError(f"写入失败：{last_error}")


def _save_once(result: AnalysisResult, settings: Settings) -> SaveResult:
    """完成一次完整写入事务。

    :param result: 编排结果。
    :param settings: 运行配置。
    :return: 写入结果。
    """
    with open_connection(settings) as connection:
        with transaction(connection) as cursor:
            if not _lock_input_versions(cursor, result):
                raise StaleReportError("输入版本已变化")
            report_id = _upsert_report(cursor, result)
            sort_day = (
                result.metadata.published_on
                if result.metadata and result.metadata.published_on
                else UNKNOWN_SORT_DAY
            )
            scope_keys: list[tuple[str, str, str]] = []
            for scope in result.scopes:
                scope_keys.append(
                    (
                        scope.product_id,
                        (scope.contract or "").upper(),
                        scope.period_key,
                    )
                )
                _write_scope(cursor, report_id, scope, sort_day)
            _delete_missing_opinions(cursor, report_id, scope_keys)
    if result.attempt_id is not None:
        try:
            finish_attempt(
                result.attempt_id,
                STATUS_SAVED,
                result_count=len(result.scopes),
                settings=settings,
            )
        except Exception:  # noqa: BLE001 - 数据已提交，状态更新失败只记日志
            LOGGER.exception(
                "更新分析状态失败：attempt_id=%s", result.attempt_id
            )
    return SaveResult(
        status=STATUS_SAVED,
        attempt_id=result.attempt_id,
        report_id=report_id,
        opinion_count=len(result.scopes),
    )


def _delete_missing_opinions(
    cursor: Any,
    report_id: int,
    scope_keys: list[tuple[str, str, str]],
) -> None:
    """删除本次结果已不包含的旧观点。

    :param cursor: 事务游标。
    :param report_id: 报告 ID。
    :param scope_keys: 保留的 ``(品种, 合约, 周期)`` 列表。
    """
    cursor.execute(
        "SELECT id, product_id, contract_key, period_key FROM opinions "
        "WHERE report_id = %s",
        (report_id,),
    )
    keep = set(scope_keys)
    stale_ids = [
        int(row["id"])
        for row in cursor.fetchall()
        if (
            str(row["product_id"]),
            str(row["contract_key"]),
            str(row["period_key"]),
        )
        not in keep
    ]
    if not stale_ids:
        return
    placeholders = ", ".join(["%s"] * len(stale_ids))
    cursor.execute(
        f"DELETE FROM opinion_evidence WHERE opinion_id IN ({placeholders})",
        tuple(stale_ids),
    )
    cursor.execute(
        f"DELETE FROM opinions WHERE id IN ({placeholders})",
        tuple(stale_ids),
    )
