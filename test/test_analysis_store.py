"""分析记录层测试：报告去重键、输入不完整与状态常量。"""

from __future__ import annotations

import datetime
import unittest

from backend.analysis_store import compute_input_hash, compute_report_group_key
from backend.models import AnalysisResult, InputPart


class ReportGroupKeyTest(unittest.TestCase):
    """报告去重键。"""

    def test_same_report_fields_produce_same_key(self) -> None:
        """机构、日期、标题一致时键稳定。"""
        first = compute_report_group_key(
            "宝城期货", datetime.date(2024, 3, 1), "有色早报", 101
        )
        second = compute_report_group_key(
            "宝城期货", datetime.date(2024, 3, 1), "有色早报", 999
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)

    def test_institution_or_date_change_produces_new_key(self) -> None:
        """机构、日期或标题变化时视为不同报告。"""
        base = compute_report_group_key(
            "宝城期货", datetime.date(2024, 3, 1), "有色早报", 101
        )
        self.assertNotEqual(
            base,
            compute_report_group_key(
                "大越期货", datetime.date(2024, 3, 1), "有色早报", 101
            ),
        )
        self.assertNotEqual(
            base,
            compute_report_group_key(
                "宝城期货", datetime.date(2024, 3, 2), "有色早报", 101
            ),
        )

    def test_empty_fields_fall_back_to_article_id(self) -> None:
        """元数据全缺时按文章 ID 兜底。"""
        first = compute_report_group_key(None, None, None, 101)
        second = compute_report_group_key(None, None, None, 102)
        self.assertNotEqual(first, second)


class InputHashTest(unittest.TestCase):
    """输入指纹。"""

    def test_missing_attachment_is_part_of_hash(self) -> None:
        """缺失附件的空指纹参与计算，附件补入后结果应过期。"""
        present = compute_input_hash(
            [
                {
                    "role": "main",
                    "relative_path": "a.html",
                    "article_id": 1,
                    "content_hash": "1" * 64,
                },
                {
                    "role": "attachment",
                    "relative_path": "b.pdf",
                    "article_id": 2,
                    "content_hash": "2" * 64,
                },
            ]
        )
        missing = compute_input_hash(
            [
                {
                    "role": "main",
                    "relative_path": "a.html",
                    "article_id": 1,
                    "content_hash": "1" * 64,
                },
                {
                    "role": "attachment",
                    "relative_path": "b.pdf",
                    "article_id": None,
                    "content_hash": None,
                },
            ]
        )
        self.assertNotEqual(present, missing)
        self.assertEqual(len(present), 64)


class SaveWithoutMainTest(unittest.TestCase):
    """缺少主文版本时拒绝写入。"""

    def test_missing_main_part_returns_failed(self) -> None:
        """不进入事务即返回 failed，避免写半条报告。"""
        from backend.analysis_store import save_opinions

        result = AnalysisResult(
            article_id=101,
            input_hash="0" * 64,
            analyzer_version="test",
            input_parts=[
                InputPart(
                    role="attachment",
                    relative_path="a.pdf",
                    article_id=2,
                    content_hash="2" * 64,
                )
            ],
        )
        saved = save_opinions(result)
        self.assertEqual(saved.status, "failed")
        self.assertEqual(saved.error_code, "input_incomplete")


if __name__ == "__main__":
    unittest.main()
