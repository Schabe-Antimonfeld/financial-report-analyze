"""查询层：列表、详情、趋势与证据原文。

FastAPI 只做参数校验与响应封装，SQL 和统计口径都放在这里，便于用真实数据
单独测试。列表按 ``(sort_day, id)`` 倒序并用游标分页；趋势按机构每天每个
口径一票统计，原文观点与模型推断各自一套分母。
"""
from __future__ import annotations

import base64
import datetime
import json
from dataclasses import dataclass
from typing import Any

from backend.config import Settings, get_settings
from backend.db import open_connection
from backend.errors import CursorError
from backend.repository import load_article
from backend.textutil import canonical_json, sha256_hex

UNKNOWN_SORT_DAY = datetime.date(1000, 1, 1)
DIRECTIONS = ("bullish", "bearish", "range")
SOURCES = ("rule", "llm")
REVIEW_STATES = ("recognized", "unresolved", "review")
CONDITIONAL_STATEMENTS = ("conditional", "scenario")


@dataclass(frozen=True)
class OpinionFilters:
    """列表筛选条件。"""

    from_date: datetime.date | None = None
    to_date: datetime.date | None = None
    institution: str | None = None
    product_id: str | None = None
    contract: str | None = None
    period_key: str | None = None
    period_definition: str | None = None
    direction: str | None = None
    source: str | None = None
    review_state: str | None = None

    def digest(self) -> str:
        """生成筛选摘要，用于校验游标是否仍属同一组筛选。

        :return: 16 位十六进制摘要。
        """
        payload = {
            "from_date": self.from_date.isoformat() if self.from_date else None,
            "to_date": self.to_date.isoformat() if self.to_date else None,
            "institution": self.institution,
            "product_id": self.product_id,
            "contract": self.contract,
            "period_key": self.period_key,
            "period_definition": self.period_definition,
            "direction": self.direction,
            "source": self.source,
            "review_state": self.review_state,
        }
        return sha256_hex(canonical_json(payload))[:16]


def encode_cursor(sort_day: datetime.date, opinion_id: int, filters: OpinionFilters) -> str:
    """编码下一页游标。

    :param sort_day: 当前行排序日。
    :param opinion_id: 当前行观点 ID。
    :param filters: 当前筛选。
    :return: URL 安全的游标字符串。
    """
    payload = {"d": sort_day.isoformat(), "i": opinion_id, "f": filters.digest()}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str, filters: OpinionFilters) -> tuple[datetime.date, int]:
    """解码并校验游标。

    :param token: 游标字符串。
    :param filters: 当前筛选。
    :return: ``(排序日, 观点 ID)``。
    :raises CursorError: 游标损坏、字段缺失或筛选已变化。
    """
    padded = token + "=" * (-len(token) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise CursorError("游标格式非法") from error
    if not isinstance(payload, dict):
        raise CursorError("游标格式非法")
    try:
        sort_day = datetime.date.fromisoformat(payload["d"])
        opinion_id = int(payload["i"])
        digest = str(payload["f"])
    except (KeyError, TypeError, ValueError) as error:
        raise CursorError("游标字段缺失") from error
    if digest != filters.digest():
        raise CursorError("筛选条件已变化，请从第一页开始")
    return sort_day, opinion_id


def _like(value: str) -> str:
    """把用户输入转成可安全用于 ``LIKE`` 的模式。

    :param value: 机构名片段。
    :return: 转义通配符后的模式。
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def build_opinion_where(
    filters: OpinionFilters,
    cursor: tuple[datetime.date, int] | None = None,
) -> tuple[list[str], list[Any]]:
    """组装参数化查询条件。

    :param filters: 筛选条件。
    :param cursor: 已解析的游标。
    :return: ``(条件列表, 参数列表)``。
    """
    conditions: list[str] = []
    parameters: list[Any] = []
    excludes_unknown_day = False
    if filters.from_date is not None:
        conditions.append("o.sort_day >= %s")
        parameters.append(filters.from_date)
        excludes_unknown_day = True
    if filters.to_date is not None:
        conditions.append("o.sort_day <= %s")
        parameters.append(filters.to_date)
        excludes_unknown_day = True
    if excludes_unknown_day:
        conditions.append("o.sort_day <> %s")
        parameters.append(UNKNOWN_SORT_DAY)
    if filters.institution:
        conditions.append("r.institution LIKE %s")
        parameters.append(_like(filters.institution))
    if filters.product_id:
        conditions.append("o.product_id = %s")
        parameters.append(filters.product_id)
    if filters.contract:
        conditions.append("o.contract_key = %s")
        parameters.append(filters.contract.upper())
    if filters.period_key:
        conditions.append("o.period_key = %s")
        parameters.append(filters.period_key)
    if filters.period_definition:
        if filters.period_definition == "unspecified":
            conditions.append("o.period_definition IS NULL")
        else:
            conditions.append("o.period_definition = %s")
            parameters.append(filters.period_definition)
    if filters.direction:
        conditions.append("o.direction = %s")
        parameters.append(filters.direction)
    if filters.source:
        conditions.append("o.source = %s")
        parameters.append(filters.source)
    if filters.review_state:
        conditions.append("o.review_state = %s")
        parameters.append(filters.review_state)
    if cursor is not None:
        sort_day, opinion_id = cursor
        conditions.append("(o.sort_day < %s OR (o.sort_day = %s AND o.id < %s))")
        parameters.extend([sort_day, sort_day, opinion_id])
    return conditions, parameters


def _json_value(value: Any) -> Any:
    """把 MySQL JSON 列转成 Python 对象。

    :param value: 驱动返回的 JSON 值。
    :return: 解析后的对象；``None`` 原样返回。
    """
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _iso_date(value: Any) -> datetime.date | None:
    """把日期列转成 ``datetime.date``。"""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def _list_item(row: dict[str, Any]) -> dict[str, Any]:
    """把一行观点转成列表项。

    :param row: 联表查询结果。
    :return: 交接 JSON 的列表项。
    """
    published_on = _iso_date(row["published_on"])
    return {
        "id": int(row["id"]),
        "institution": row["institution"],
        "title": row["title"],
        "published_on": published_on.isoformat() if published_on else None,
        "date_source": row["date_source"],
        "product_id": row["product_id"],
        "contract": row["contract_key"] or None,
        "period_key": row["period_key"],
        "period_text": row["period_text"],
        "period_definition": row["period_definition"],
        "direction": row["direction"],
        "source": row["source"],
        "statement_type": row["statement_type"],
        "review_state": row["review_state"],
    }


LIST_COLUMNS = """
    o.id, o.product_id, o.contract_key, o.period_key, o.period_text,
    o.period_definition, o.direction, o.source, o.statement_type,
    o.review_state, o.sort_day, r.institution, r.title, r.published_on,
    r.date_source
"""


def list_opinions(
    filters: OpinionFilters,
    *,
    cursor: str | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """查询观点列表一页。

    :param filters: 筛选条件。
    :param cursor: 上一页返回的游标。
    :param limit: 每页条数，默认 50、最多 100。
    :param settings: 运行配置。
    :return: ``{"items": [...], "next_cursor": str | None}``。
    :raises CursorError: 游标非法或与筛选不一致。
    """
    settings = settings or get_settings()
    page_size = limit or settings.api_page_size
    page_size = max(1, min(page_size, settings.api_max_page_size))
    decoded = decode_cursor(cursor, filters) if cursor else None
    conditions, parameters = build_opinion_where(filters, decoded)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    statement = f"""
        SELECT {LIST_COLUMNS}
        FROM opinions AS o
        JOIN reports AS r ON r.id = o.report_id
        {where_clause}
        ORDER BY o.sort_day DESC, o.id DESC
        LIMIT %s
    """
    parameters.append(page_size + 1)
    with open_connection(settings, autocommit=True) as connection:
        cursor_object = connection.cursor(dictionary=True)
        try:
            cursor_object.execute(statement, tuple(parameters))
            rows = cursor_object.fetchall()
        finally:
            cursor_object.close()
    has_more = len(rows) > page_size
    visible = rows[:page_size]
    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        next_cursor = encode_cursor(
            _iso_date(last["sort_day"]) or UNKNOWN_SORT_DAY,
            int(last["id"]),
            filters,
        )
    return {
        "items": [_list_item(row) for row in visible],
        "next_cursor": next_cursor,
    }


def get_opinion(
    opinion_id: int,
    *,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """查询一条观点详情及其证据。

    :param opinion_id: 观点 ID。
    :param settings: 运行配置。
    :return: 详情字典，不存在时为 ``None``。
    """
    settings = settings or get_settings()
    with open_connection(settings, autocommit=True) as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT o.id, o.product_id, o.contract_key, o.period_key,
                       o.period_text, o.period_definition, o.direction,
                       o.source, o.statement_type, o.review_state,
                       o.reason_code, o.outlook_text, o.conditions,
                       o.inference_reason, r.institution, r.title,
                       r.published_on, r.date_source
                FROM opinions AS o
                JOIN reports AS r ON r.id = o.report_id
                WHERE o.id = %s
                """,
                (opinion_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                SELECT id, kind, rule_id, grade, quote, block_id,
                       raw_article_id, LOWER(HEX(content_hash)) AS content_hash,
                       location, attachment_chain, contract
                FROM opinion_evidence
                WHERE opinion_id = %s
                ORDER BY id
                """,
                (opinion_id,),
            )
            evidence_rows = cursor.fetchall()
        finally:
            cursor.close()
    detail = _list_item(row)
    detail.update(
        {
            "reason_code": row["reason_code"],
            "outlook_text": row["outlook_text"],
            "inference_reason": row["inference_reason"],
            "conditions": _json_value(row["conditions"]) or [],
            "evidence": [
                {
                    "id": int(item["id"]),
                    "kind": item["kind"],
                    "rule_id": item["rule_id"],
                    "grade": item["grade"],
                    "quote": item["quote"],
                    "block_id": item["block_id"],
                    "raw_article_id": int(item["raw_article_id"]),
                    "content_hash": item["content_hash"],
                    "location": _json_value(item["location"]) or {},
                    "attachment_chain": _json_value(item["attachment_chain"]) or [],
                    "contract": item["contract"],
                }
                for item in evidence_rows
            ],
        }
    )
    return detail


def aggregate_trends(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按机构每天一个口径一票汇总趋势点。

    :param rows: 已按品种、周期、来源、日期范围筛选的观点行。
    :return: 按日期与期限定义排序的日度点列表。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        group_key = row.get("report_group_key")
        if isinstance(group_key, (bytes, bytearray)):
            group_key = group_key.hex()
        key = str(group_key or f"article:{row['raw_article_id']}")
        grouped.setdefault(key, []).append(row)

    votes: dict[tuple[datetime.date, str | None, str], dict[str, Any]] = {}
    for reports in grouped.values():
        outcome = _report_outcome(reports)
        sort_day = _iso_date(reports[0]["published_on"]) or UNKNOWN_SORT_DAY
        institution = reports[0]["institution"] or ""
        bucket_key = (sort_day, outcome["definition"], institution)
        bucket = votes.setdefault(
            bucket_key,
            {
                "date": sort_day,
                "period_definition": outcome["definition"],
                "institution": institution,
                "reports": 0,
                "directions": set(),
                "has_conditional": False,
                "has_unresolved": False,
            },
        )
        bucket["reports"] += 1
        if outcome["direction"]:
            bucket["directions"].add(outcome["direction"])
        if outcome["conditional"]:
            bucket["has_conditional"] = True
        if outcome["direction"] is None and not outcome["conditional"]:
            bucket["has_unresolved"] = True

    points: dict[tuple[datetime.date, str | None], dict[str, Any]] = {}
    for (sort_day, definition, _institution), bucket in votes.items():
        point = points.setdefault(
            (sort_day, definition),
            {
                "date": sort_day,
                "period_definition": definition,
                "bullish_count": 0,
                "bearish_count": 0,
                "range_count": 0,
                "mixed_count": 0,
                "unresolved_count": 0,
                "conditional_count": 0,
                "report_count": 0,
                "institution_count": 0,
            },
        )
        point["report_count"] += bucket["reports"]
        point["institution_count"] += 1
        directions = bucket["directions"]
        if len(directions) > 1:
            point["mixed_count"] += 1
        elif len(directions) == 1:
            direction = next(iter(directions))
            point[f"{direction}_count"] += 1
        if bucket["has_conditional"]:
            point["conditional_count"] += 1
        if bucket["has_unresolved"]:
            point["unresolved_count"] += 1

    result = []
    for point in points.values():
        denominator = (
            point["bullish_count"]
            + point["bearish_count"]
            + point["range_count"]
        )
        if denominator:
            ratios = {
                "bullish": point["bullish_count"] / denominator,
                "bearish": point["bearish_count"] / denominator,
                "range": point["range_count"] / denominator,
            }
        else:
            ratios = {"bullish": None, "bearish": None, "range": None}
        point["ratios"] = ratios
        point["date"] = point["date"].isoformat()
        result.append(point)
    result.sort(
        key=lambda item: (
            item["date"],
            item["period_definition"] is not None,
            item["period_definition"] or "",
        )
    )
    return result


def _report_outcome(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把一个报告组收敛成一条投票结论。

    :param rows: 同一报告组在同一期限下的观点行。
    :return: 含 ``direction``、``conditional``、``definition`` 的结论。
    """
    definition = rows[0].get("period_definition")
    product_level = [row for row in rows if not row.get("contract_key")]
    candidates = product_level or rows
    directions = {
        str(row["direction"])
        for row in candidates
        if row.get("direction") in DIRECTIONS
    }
    conditional = any(
        row.get("statement_type") in CONDITIONAL_STATEMENTS for row in rows
    ) and not directions
    direction = None
    if len(directions) == 1:
        direction = next(iter(directions))
    return {
        "definition": definition,
        "direction": direction,
        "mixed": len(directions) > 1,
        "conditional": conditional,
    }


def get_trend_points(
    *,
    product_id: str,
    period_key: str,
    from_date: datetime.date,
    to_date: datetime.date,
    contract: str | None = None,
    source: str = "rule",
    period_definition: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """查询某品种、周期、来源的日度分布。

    :param product_id: 标准品种 ID。
    :param period_key: 规范周期键。
    :param from_date: 起始日期，含当天。
    :param to_date: 结束日期，含当天。
    :param contract: 可选合约。
    :param source: ``rule`` 或 ``llm``。
    :param period_definition: 期限定义；``unspecified`` 表示空定义。
    :param settings: 运行配置。
    :return: ``{"source": source, "points": [...]}``。
    """
    settings = settings or get_settings()
    conditions = [
        "o.product_id = %s",
        "o.period_key = %s",
        "o.source = %s",
        "o.sort_day BETWEEN %s AND %s",
        "o.review_state <> %s",
        "r.metadata_review = 0",
    ]
    parameters: list[Any] = [product_id, period_key, source, from_date, to_date, "review"]
    if contract:
        conditions.append("o.contract_key = %s")
        parameters.append(contract.upper())
    if period_definition:
        if period_definition == "unspecified":
            conditions.append("o.period_definition IS NULL")
        else:
            conditions.append("o.period_definition = %s")
            parameters.append(period_definition)
    statement = f"""
        SELECT o.id, o.contract_key, o.period_definition, o.direction,
               o.source, o.statement_type, o.review_state,
               r.institution, r.published_on, r.report_group_key,
               r.raw_article_id
        FROM opinions AS o
        JOIN reports AS r ON r.id = o.report_id
        WHERE {' AND '.join(conditions)}
        ORDER BY o.sort_day, o.id
    """
    with open_connection(settings, autocommit=True) as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(statement, tuple(parameters))
            rows = cursor.fetchall()
        finally:
            cursor.close()
    return {"source": source, "points": aggregate_trends(rows)}


def get_evidence_source(
    evidence_id: int,
    *,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """读取证据对应的原文版本。

    :param evidence_id: 证据 ID。
    :param settings: 运行配置。
    :return: ``{"format", "content", "quote", "heading_path", "media_type"}``，
        不存在时为 ``None``。
    :raises ArticleNotFoundError: 原文版本已不可读。
    """
    settings = settings or get_settings()
    with open_connection(settings, autocommit=True) as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, raw_article_id, LOWER(HEX(content_hash)) AS content_hash,
                       quote, location
                FROM opinion_evidence
                WHERE id = %s
                """,
                (evidence_id,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
    if row is None:
        return None
    article = load_article(
        int(row["raw_article_id"]),
        str(row["content_hash"]),
        settings=settings,
    )
    location = _json_value(row["location"]) or {}
    heading_path = location.get("heading_path") or []
    media_types = {
        "pdf": "application/pdf",
        "png": "image/png",
        "html": "application/json",
    }
    return {
        "format": article.format,
        "content": article.content,
        "quote": row["quote"],
        "heading_path": heading_path,
        "location": location,
        "media_type": media_types.get(article.format, "application/octet-stream"),
    }


def count_reports(
    *,
    settings: Settings | None = None,
) -> int:
    """统计已提交报告数，用于健康检查。

    :param settings: 运行配置。
    :return: 报告条数。
    """
    settings = settings or get_settings()
    with open_connection(settings, autocommit=True) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM reports")
            row = cursor.fetchone()
        finally:
            cursor.close()
    return int(row[0]) if row else 0
