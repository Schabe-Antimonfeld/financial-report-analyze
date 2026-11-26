"""查询层单元测试：游标、筛选条件与日度聚合口径。"""

from __future__ import annotations

import datetime
import unittest

from backend.api_queries import (
    OpinionFilters,
    UNKNOWN_SORT_DAY,
    aggregate_trends,
    build_opinion_where,
    decode_cursor,
    encode_cursor,
)
from backend.errors import CursorError

DAY = datetime.date(2024, 3, 1)


def make_row(
    *,
    group: str = "g1",
    institution: str = "宝城期货",
    direction: str | None = "bullish",
    contract_key: str = "",
    definition: str | None = "<=1w",
    statement: str = "unconditional",
    source: str = "rule",
    day: datetime.date = DAY,
    article_id: int = 1,
) -> dict[str, object]:
    """构造一条聚合输入行。"""
    return {
        "report_group_key": group.encode("utf-8").ljust(32, b"\x00"),
        "raw_article_id": article_id,
        "institution": institution,
        "published_on": day,
        "direction": direction,
        "contract_key": contract_key,
        "period_definition": definition,
        "statement_type": statement,
        "source": source,
        "review_state": "recognized",
        "id": article_id,
    }


class CursorTest(unittest.TestCase):
    """游标编解码与筛选绑定。"""

    def test_roundtrip(self) -> None:
        """编码后能还原日期与 ID。"""
        filters = OpinionFilters(product_id="cu")
        token = encode_cursor(DAY, 501, filters)
        self.assertEqual(decode_cursor(token, filters), (DAY, 501))

    def test_filter_change_is_rejected(self) -> None:
        """筛选变化后旧游标不能继续使用。"""
        token = encode_cursor(DAY, 501, OpinionFilters(product_id="cu"))
        with self.assertRaises(CursorError):
            decode_cursor(token, OpinionFilters(product_id="al"))

    def test_broken_token_is_rejected(self) -> None:
        """非法 base64 与缺字段都报游标错误。"""
        with self.assertRaises(CursorError):
            decode_cursor("not-base64!!", OpinionFilters())
        with self.assertRaises(CursorError):
            decode_cursor("eyJkIjoiMjAyNC0wMy0wMSJ9", OpinionFilters())

    def test_unknown_day_cursor(self) -> None:
        """未知发布日期用内部最早日期作游标值。"""
        filters = OpinionFilters()
        token = encode_cursor(UNKNOWN_SORT_DAY, 9, filters)
        self.assertEqual(decode_cursor(token, filters), (UNKNOWN_SORT_DAY, 9))


class WhereBuilderTest(unittest.TestCase):
    """参数化筛选条件。"""

    def test_date_range_excludes_unknown_day_once(self) -> None:
        """显式日期筛选排除内部占位日期，且只加一次条件。"""
        conditions, parameters = build_opinion_where(
            OpinionFilters(from_date=DAY, to_date=DAY)
        )
        self.assertEqual(conditions.count("o.sort_day <> %s"), 1)
        self.assertEqual(parameters.count(UNKNOWN_SORT_DAY), 1)
        self.assertEqual(conditions.count("o.sort_day >= %s"), 1)
        self.assertEqual(conditions.count("o.sort_day <= %s"), 1)

    def test_declared_filters_are_parameterized(self) -> None:
        """合约统一大写，机构模糊匹配，期限 unspecified 走 IS NULL。"""
        conditions, parameters = build_opinion_where(
            OpinionFilters(
                institution="宝城",
                contract="cu2404",
                product_id="cu",
                period_key="short_term",
                period_definition="unspecified",
                direction="bullish",
                source="rule",
                review_state="recognized",
            )
        )
        self.assertIn("o.contract_key = %s", conditions)
        self.assertIn("cu2404".upper(), parameters)
        self.assertIn("r.institution LIKE %s", conditions)
        self.assertIn("%宝城%", parameters)
        self.assertIn("o.period_definition IS NULL", conditions)
        self.assertIn("o.review_state = %s", conditions)

    def test_cursor_adds_keyset_condition(self) -> None:
        """游标使用 ``(sort_day, id)`` 键集条件。"""
        conditions, parameters = build_opinion_where(
            OpinionFilters(), (DAY, 501)
        )
        self.assertIn("(o.sort_day < %s OR (o.sort_day = %s AND o.id < %s))", conditions)
        self.assertEqual(parameters[-3:], [DAY, DAY, 501])


class AggregateTrendsTest(unittest.TestCase):
    """日度分布口径。"""

    def test_one_vote_per_institution_per_day(self) -> None:
        """两个机构一天一多一空，各计一票，比例为各半。"""
        rows = [
            make_row(group="g1", institution="宝城期货", direction="bullish"),
            make_row(group="g2", institution="大越期货", direction="bearish"),
        ]
        points = aggregate_trends(rows)
        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual(point["date"], "2024-03-01")
        self.assertEqual((point["bullish_count"], point["bearish_count"]), (1, 1))
        self.assertEqual(point["institution_count"], 2)
        self.assertEqual(point["report_count"], 2)
        self.assertEqual(point["ratios"], {"bullish": 0.5, "bearish": 0.5, "range": 0.0})

    def test_same_institution_conflict_counts_as_mixed(self) -> None:
        """同机构同口径方向冲突只计一票分歧，不进方向分母。"""
        rows = [
            make_row(group="g1", direction="bullish"),
            make_row(group="g2", direction="bearish"),
        ]
        point = aggregate_trends(rows)[0]
        self.assertEqual(point["mixed_count"], 1)
        self.assertEqual(
            point["bullish_count"] + point["bearish_count"] + point["range_count"], 0
        )
        self.assertEqual(point["ratios"], {"bullish": None, "bearish": None, "range": None})
        self.assertEqual(point["institution_count"], 1)
        self.assertEqual(point["report_count"], 2)

    def test_product_level_view_wins_over_contracts(self) -> None:
        """同时有品种级判断时优先采用，不因合约方向不同计分歧。"""
        rows = [
            make_row(group="g1", contract_key="", direction="bullish"),
            make_row(group="g1", contract_key="CU2404", direction="bearish"),
        ]
        point = aggregate_trends(rows)[0]
        self.assertEqual(point["bullish_count"], 1)
        self.assertEqual(point["mixed_count"], 0)
        self.assertEqual(point["report_count"], 1)

    def test_duplicate_report_group_counts_once(self) -> None:
        """同一报告组的多条合约观点只算一份报告、一票。"""
        rows = [
            make_row(group="g1", contract_key="CU2404", direction="bullish"),
            make_row(group="g1", contract_key="CU2405", direction="bullish"),
        ]
        point = aggregate_trends(rows)[0]
        self.assertEqual(point["report_count"], 1)
        self.assertEqual(point["bullish_count"], 1)

    def test_conditional_and_unresolved_are_coverage_only(self) -> None:
        """条件观点与未明确只计覆盖，不进入多空震荡分母。"""
        rows = [
            make_row(group="g1", direction=None, statement="conditional"),
            make_row(group="g2", direction=None, statement="none"),
        ]
        point = aggregate_trends(rows)[0]
        self.assertEqual(point["conditional_count"], 1)
        self.assertEqual(point["unresolved_count"], 1)
        self.assertEqual(point["ratios"]["bullish"], None)
        self.assertEqual(point["institution_count"], 1)

    def test_period_definitions_are_separate_series(self) -> None:
        """同名周期不同定义拆成不同系列。"""
        rows = [
            make_row(group="g1", definition="<=1w"),
            make_row(group="g2", definition="2w-1mo"),
        ]
        points = aggregate_trends(rows)
        self.assertEqual(
            {point["period_definition"] for point in points}, {"<=1w", "2w-1mo"}
        )
        self.assertTrue(all(point["bullish_count"] == 1 for point in points))

    def test_null_definition_is_kept(self) -> None:
        """没有期限定义的观点单独成系列。"""
        point = aggregate_trends([make_row(definition=None)])[0]
        self.assertIsNone(point["period_definition"])
        self.assertEqual(point["date"], "2024-03-01")

    def test_empty_input_returns_no_points(self) -> None:
        """没有数据时不构造假 0% 点。"""
        self.assertEqual(aggregate_trends([]), [])


if __name__ == "__main__":
    unittest.main()
