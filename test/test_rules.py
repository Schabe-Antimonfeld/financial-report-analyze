"""规则层单元测试。"""
from __future__ import annotations

import datetime
import unittest
from typing import Any

from backend.models import Block, RawArticle, ReportMetadata
from backend.rules import direction_matches, extract_metadata, extract_opinions


def _block(
    block_id: str,
    order: int,
    kind: str,
    text: str,
    *,
    heading_path: list[str] | None = None,
    table: dict[str, Any] | None = None,
) -> Block:
    """构造测试用块。

    :param block_id: 块 ID。
    :param order: 阅读顺序。
    :param kind: 块类型。
    :param text: 原文。
    :param heading_path: 标题路径。
    :param table: 表格信息。
    :return: 块对象。
    """
    return Block(
        block_id=block_id,
        order=order,
        raw_article_id=1,
        content_hash="a" * 64,
        kind=kind,
        text=text,
        heading_path=list(heading_path or []),
        location={"kind": kind, "page": 1},
        table=table,
    )


def _article(
    relative_path: str = "研报/铜早报.docx",
    collected_on: datetime.date | None = None,
    institution_hint: str | None = None,
) -> RawArticle:
    """构造测试用原文记录。

    :param relative_path: 相对路径。
    :param collected_on: 采集日期。
    :param institution_hint: 机构提示。
    :return: 原文记录。
    """
    return RawArticle(
        article_id=1,
        relative_path=relative_path,
        content_hash="b" * 64,
        format="docx",
        content=b"test",
        collected_on=collected_on,
        institution_hint=institution_hint,
    )


def _fallback_eligible(scope: Any) -> bool:
    """模拟 plan_fallback 的兜底过滤条件。

    :param scope: 范围结果。
    :return: 是否应交给模型兜底。
    """
    return (
        scope.direction is None
        and scope.target_reliable
        and scope.statement_type == "none"
        and scope.review_state == "unresolved"
    )


class TestDirectionMatches(unittest.TestCase):
    """方向词表匹配测试。"""

    def test_prefers_longest_phrase(self) -> None:
        """同一位置命中多个词时保留最长词。"""
        self.assertEqual(
            direction_matches("震荡偏强"),
            [("震荡偏强", "bullish")],
        )

    def test_matches_contained_phrase(self) -> None:
        """长句中的方向词只返回实际命中的词。"""
        self.assertEqual(
            direction_matches("预计本周矿价维持震荡运行"),
            [("维持震荡", "range")],
        )


class TestExtractMetadata(unittest.TestCase):
    """报告元数据测试。"""

    def test_directory_fallback(self) -> None:
        """没有发布日时退回采集日期并标明目录来源。"""
        article = _article(
            relative_path="研报/铜早报.docx",
            collected_on=datetime.date(2024, 3, 5),
            institution_hint="宝城期货",
        )
        blocks = [
            _block("b-0001", 1, "heading", "铜早报"),
            _block("b-0002", 2, "metadata", "宝城期货"),
        ]
        metadata = extract_metadata(article, blocks)
        self.assertEqual(metadata.institution, "宝城期货")
        self.assertEqual(metadata.title, "铜早报")
        self.assertEqual(metadata.published_on, datetime.date(2024, 3, 5))
        self.assertEqual(metadata.date_source, "directory")
        self.assertEqual(metadata.field_sources["institution"], {"block_id": "b-0002"})
        self.assertEqual(metadata.field_sources["title"], {"block_id": "b-0001"})
        self.assertEqual(
            metadata.field_sources["published_on"],
            {"source": "directory"},
        )
        self.assertEqual(metadata.issues, [])

    def test_title_date_sets_publication(self) -> None:
        """标题中的 ISO 日期作为发布日。"""
        article = _article(
            relative_path="研报/铜早报.docx",
            collected_on=datetime.date(2024, 3, 5),
        )
        blocks = [
            _block("b-0001", 1, "heading", "2024-03-01 铜早报"),
            _block("b-0002", 2, "metadata", "宝城期货"),
        ]
        metadata = extract_metadata(article, blocks)
        self.assertEqual(metadata.published_on, datetime.date(2024, 3, 1))
        self.assertEqual(metadata.date_source, "publication")
        self.assertEqual(
            metadata.field_sources["published_on"],
            {"block_id": "b-0001"},
        )

    def test_chinese_date_in_metadata(self) -> None:
        """元数据块中的中文日期同样识别。"""
        article = _article(relative_path="研报/铜早报.docx")
        blocks = [
            _block("b-0001", 1, "heading", "铜早报"),
            _block("b-0002", 2, "metadata", "发布日期：2024年3月1日"),
        ]
        metadata = extract_metadata(article, blocks)
        self.assertEqual(metadata.published_on, datetime.date(2024, 3, 1))
        self.assertEqual(metadata.date_source, "publication")

    def test_institution_conflict_adds_issue(self) -> None:
        """两个不同机构表述才记录元数据复核问题。"""
        article = _article(relative_path="研报/铜早报.docx")
        blocks = [
            _block("b-0001", 1, "metadata", "宝城期货"),
            _block("b-0002", 2, "metadata", "大越期货"),
        ]
        metadata = extract_metadata(article, blocks)
        self.assertEqual(metadata.institution, "宝城期货")
        self.assertTrue(
            any(issue.code == "metadata_review" for issue in metadata.issues)
        )


class TestExtractOpinions(unittest.TestCase):
    """观点规则测试。"""

    def test_table_cells_build_three_period_scopes(self) -> None:
        """宝城铜 2404 的三个周期单元格各自成范围。"""
        blocks = [
            _block(
                "b-0001",
                1,
                "table_cell",
                "上涨",
                table={
                    "row_label": "铜",
                    "contract_text": "2404",
                    "headers": ["短期"],
                },
            ),
            _block(
                "b-0002",
                2,
                "table_cell",
                "震荡",
                table={
                    "row_label": "铜",
                    "contract_text": "2404",
                    "headers": ["中期"],
                },
            ),
            _block(
                "b-0003",
                3,
                "table_cell",
                "上涨",
                table={
                    "row_label": "铜",
                    "contract_text": "2404",
                    "headers": ["日内"],
                },
            ),
        ]
        result = extract_opinions(blocks, ReportMetadata())
        scopes = {scope.scope_id: scope for scope in result.scopes}
        self.assertEqual(
            set(scopes),
            {
                "cu|CU2404|short_term",
                "cu|CU2404|medium_term",
                "cu|CU2404|intraday",
            },
        )
        expected = {
            "cu|CU2404|short_term": ("bullish", "短期", "<=1w"),
            "cu|CU2404|medium_term": ("range", "中期", "2w-1mo"),
            "cu|CU2404|intraday": ("bullish", "日内", "当天"),
        }
        for scope_id, (direction, period_text, definition) in expected.items():
            scope = scopes[scope_id]
            self.assertEqual(scope.direction, direction)
            self.assertEqual(scope.contract, "CU2404")
            self.assertEqual(scope.period_text, period_text)
            self.assertEqual(scope.period_definition, definition)
            self.assertEqual(scope.statement_type, "unconditional")
            self.assertEqual(scope.review_state, "recognized")
            self.assertEqual(scope.source, "rule")
            self.assertTrue(scope.target_reliable)
            evidence = scope.evidence[0]
            self.assertEqual(evidence.kind, "outlook")
            self.assertEqual(evidence.rule_id, "table_direction")
            self.assertEqual(evidence.grade, "A")
            self.assertEqual(evidence.quote, scope.outlook_text)
            self.assertEqual(evidence.location["heading_path"], [])

    def test_paragraph_and_strategy(self) -> None:
        """大越铁矿石段落给出本周震荡并保留操作建议。"""
        text = "预计本周矿价维持震荡运行。I2405：890-915区间逢高抛空，止损925。"
        blocks = [
            _block("b-0001", 1, "paragraph", text, heading_path=["铁矿石"]),
        ]
        result = extract_opinions(blocks, ReportMetadata())
        scopes = {scope.scope_id: scope for scope in result.scopes}
        self.assertEqual(set(scopes), {"iron_ore||week:this"})
        scope = scopes["iron_ore||week:this"]
        self.assertEqual(scope.direction, "range")
        self.assertEqual(scope.statement_type, "unconditional")
        self.assertEqual(scope.review_state, "recognized")
        self.assertEqual(scope.source, "rule")
        self.assertEqual(scope.period_text, "本周")
        self.assertEqual(scope.period_definition, "本周")
        strategy = [item for item in scope.evidence if item.kind == "strategy"]
        self.assertEqual(len(strategy), 1)
        self.assertEqual(strategy[0].contract, "I2405")
        self.assertIn("逢高抛空", strategy[0].quote)

    def test_past_background_is_ignored(self) -> None:
        """昨日行情不作观点，只保留预计分句。"""
        blocks = [
            _block(
                "b-0001",
                1,
                "paragraph",
                "昨日铜价上涨，预计铜价下跌。",
                heading_path=["铜"],
            ),
        ]
        result = extract_opinions(blocks, ReportMetadata())
        scopes = {scope.scope_id: scope for scope in result.scopes}
        self.assertEqual(set(scopes), {"cu||unspecified"})
        scope = scopes["cu||unspecified"]
        self.assertEqual(scope.direction, "bearish")
        self.assertEqual(scope.outlook_text, "预计铜价下跌")
        self.assertFalse(any("昨日" in item.quote for item in scope.evidence))

    def test_opposite_conditional_branches_become_scenario(self) -> None:
        """两个相反条件分支只记录情景，不给出主方向。"""
        blocks = [
            _block(
                "b-0001",
                1,
                "paragraph",
                "若突破 70000，则看涨；若跌破 68000，则看跌。",
                heading_path=["铜"],
            ),
        ]
        result = extract_opinions(blocks, ReportMetadata())
        scopes = {scope.scope_id: scope for scope in result.scopes}
        scope = scopes["cu||unspecified"]
        self.assertIsNone(scope.direction)
        self.assertEqual(scope.statement_type, "scenario")
        self.assertEqual(scope.review_state, "unresolved")
        self.assertEqual(len(scope.conditions), 2)
        self.assertEqual(
            {item["direction"] for item in scope.conditions},
            {"bullish", "bearish"},
        )
        self.assertFalse(_fallback_eligible(scope))

    def test_negated_view_is_never_inverted(self) -> None:
        """否定表述不反转方向，只记录复核原因。"""
        blocks = [
            _block("b-0001", 1, "paragraph", "不看涨", heading_path=["铜"]),
        ]
        result = extract_opinions(blocks, ReportMetadata())
        scopes = {scope.scope_id: scope for scope in result.scopes}
        scope = scopes["cu||unspecified"]
        self.assertIsNone(scope.direction)
        self.assertEqual(scope.review_state, "review")
        self.assertEqual(scope.reason_code, "negated_view")
        self.assertFalse(any(item.direction == "bearish" for item in result.scopes))

    def test_same_level_opposite_directions_conflict(self) -> None:
        """同等级相反方向标为复核冲突并保留双方证据。"""
        blocks = [
            _block("b-0001", 1, "paragraph", "铜价看涨。", heading_path=["铜"]),
            _block("b-0002", 2, "paragraph", "铜价看跌。", heading_path=["铜"]),
        ]
        result = extract_opinions(blocks, ReportMetadata())
        scopes = {scope.scope_id: scope for scope in result.scopes}
        scope = scopes["cu||unspecified"]
        self.assertIsNone(scope.direction)
        self.assertEqual(scope.review_state, "review")
        self.assertEqual(scope.reason_code, "direction_conflict")
        self.assertGreaterEqual(len(scope.evidence), 2)
        self.assertTrue(
            any(issue.code == "direction_conflict" for issue in result.issues)
        )

    def test_no_explicit_view_fallback(self) -> None:
        """只有背景描述时留下带原文的兜底候选。"""
        text = "铜现货库存持续下降，现货成交改善。"
        blocks = [_block("b-0001", 1, "paragraph", text, heading_path=["铜"])]
        result = extract_opinions(blocks, ReportMetadata())
        scopes = {scope.scope_id: scope for scope in result.scopes}
        scope = scopes["cu||unspecified"]
        self.assertIsNone(scope.direction)
        self.assertIsNone(scope.source)
        self.assertEqual(scope.statement_type, "none")
        self.assertEqual(scope.review_state, "unresolved")
        self.assertEqual(scope.reason_code, "no_explicit_view")
        self.assertTrue(scope.target_reliable)
        self.assertGreaterEqual(len(scope.fallback_blocks), 1)
        self.assertLessEqual(len(scope.fallback_blocks), 3)
        self.assertEqual(scope.fallback_blocks[0].text, text)
        self.assertTrue(_fallback_eligible(scope))

    def test_scope_order_is_deterministic(self) -> None:
        """打乱输入块顺序后结果顺序与证据顺序不变。"""
        blocks = [
            _block(
                "b-0001",
                1,
                "table_cell",
                "上涨",
                table={
                    "row_label": "铜",
                    "contract_text": "2404",
                    "headers": ["短期"],
                },
            ),
            _block(
                "b-0002",
                2,
                "paragraph",
                "预计铜价上涨。",
                heading_path=["铜"],
            ),
            _block(
                "b-0003",
                3,
                "paragraph",
                "锌现货库存下降。",
                heading_path=["锌"],
            ),
        ]
        first = extract_opinions(blocks, ReportMetadata())
        second = extract_opinions(list(reversed(blocks)), ReportMetadata())
        self.assertEqual(
            [scope.scope_id for scope in first.scopes],
            [scope.scope_id for scope in second.scopes],
        )
        self.assertEqual(
            [[item.quote for item in scope.evidence] for scope in first.scopes],
            [[item.quote for item in scope.evidence] for scope in second.scopes],
        )
        self.assertEqual(first.scopes[-1].reason_code, "no_explicit_view")


class TestTableContractNormalization(unittest.TestCase):
    """品种列同时承载合约号时的规范化。"""

    def _scope_for(self, row_label: str, contract_text: str | None) -> Any:
        """执行一次单格规则并返回唯一范围。

        :param row_label: 表格行标签。
        :param contract_text: 表格记录的合约文本。
        :return: 范围结果。
        """
        block = _block(
            "b-0001",
            1,
            "table_cell",
            "上涨",
            table={
                "row": 2,
                "col": 2,
                "headers": ["走势", "短期"],
                "row_label": row_label,
                "contract_text": contract_text,
            },
        )
        result = extract_opinions([block], ReportMetadata())
        self.assertEqual(len(result.scopes), 1)
        return result.scopes[0]

    def test_row_label_with_digits(self) -> None:
        """「铜2404」应得到 ``cu|CU2404|short_term``。"""
        scope = self._scope_for("铜2404", None)
        self.assertEqual(scope.scope_id, "cu|CU2404|short_term")
        self.assertEqual(scope.direction, "bullish")

    def test_row_label_with_code_prefix(self) -> None:
        """「CU2404」按代码前缀识别品种与合约。"""
        scope = self._scope_for("CU2404", None)
        self.assertEqual(scope.scope_id, "cu|CU2404|short_term")
        self.assertEqual(scope.direction, "bullish")

    def test_separate_contract_text(self) -> None:
        """「铜」加独立合约列「2404」同样组合。"""
        scope = self._scope_for("铜", "2404")
        self.assertEqual(scope.scope_id, "cu|CU2404|short_term")

    def test_invalid_month_falls_back_to_product_level(self) -> None:
        """末两位不是 01～12 时不生成合约。"""
        scope = self._scope_for("铜", "9999")
        self.assertEqual(scope.scope_id, "cu||short_term")


class TestProductDisambiguation(unittest.TestCase):
    """品种别名歧义与新词表覆盖。"""

    def _scopes(self, text: str, heading: str) -> list[Any]:
        """跑一遍规则并返回范围列表。

        :param text: 正文。
        :param heading: 标题路径。
        :return: 范围列表。
        """
        blocks = [_block("b-0001", 1, "paragraph", text, heading_path=[heading])]
        return extract_opinions(blocks, ReportMetadata()).scopes

    def test_alumina_is_not_aluminum(self) -> None:
        """「氧化铝」应归到 ``ao``，不能误匹配成 ``al``。"""
        scopes = self._scopes("氧化铝方面，预计短期偏强。", "氧化铝")
        scope_ids = [scope.scope_id for scope in scopes]
        self.assertTrue(any(scope.startswith("ao|") for scope in scope_ids))
        self.assertFalse(any(scope.startswith("al|") for scope in scope_ids))

    def test_new_products_are_recognized(self) -> None:
        """甲醇、生猪、国际铜、低硫燃料油都能识别。"""
        cases = {
            "甲醇预计上涨，生猪维持震荡。": ("ma|", "lh|"),
            "国际铜预计上涨，低硫燃料油偏弱。": ("bc|", "lu|"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                scope_ids = [scope.scope_id for scope in self._scopes(text, "品种")]
                for prefix in expected:
                    self.assertTrue(any(item.startswith(prefix) for item in scope_ids))

    def test_longer_alias_wins(self) -> None:
        """「玉米淀粉」不被截成「玉米」。"""
        scope_ids = [scope.scope_id for scope in self._scopes("玉米淀粉预计上涨。", "品种")]
        self.assertTrue(any(item.startswith("cs|") for item in scope_ids))
        self.assertFalse(any(item.startswith("c|") for item in scope_ids))

    def test_limited_momentum_is_not_bullish(self) -> None:
        """「缺乏上涨动力」「上涨空间有限」不得记成看多。"""
        cases = [
            ("电解铝短线受消费压制缺乏上涨动力。", "铝"),
            ("铜价上涨动力不足。", "铜"),
            ("预计铜价上涨空间有限。", "铜"),
        ]
        for text, heading in cases:
            with self.subTest(text=text):
                scopes = self._scopes(text, heading)
                self.assertFalse(any(scope.direction == "bullish" for scope in scopes))
                self.assertTrue(
                    any(scope.reason_code == "negated_view" for scope in scopes)
                )


if __name__ == "__main__":
    unittest.main()
