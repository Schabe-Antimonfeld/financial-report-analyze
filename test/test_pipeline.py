"""编排层单元测试：附件路径、输入指纹、报告汇总与兜底合并。"""

from __future__ import annotations

import unittest
from unittest import mock

from backend import pipeline
from backend.config import Settings
from backend.errors import AttachmentPathError
from backend.models import (
    AttachmentRef,
    Block,
    Evidence,
    ExtractionResult,
    FallbackResult,
    RawArticle,
    ScopeResult,
)


def make_part(
    role: str,
    relative_path: str,
    article_id: int | None,
    content_hash: str | None,
) -> dict[str, object]:
    """构造一个输入版本字典。"""
    return {
        "role": role,
        "relative_path": relative_path,
        "article_id": article_id,
        "content_hash": content_hash,
    }


def make_block(
    block_id: str,
    text: str,
    *,
    order: int = 1,
    article_id: int = 1,
    kind: str = "paragraph",
) -> Block:
    """构造测试用文本块。"""
    return Block(
        block_id=block_id,
        order=order,
        raw_article_id=article_id,
        content_hash="a" * 64,
        kind=kind,
        text=text,
        location={"format": "html", "element_index": order},
    )


class AttachmentPathTest(unittest.TestCase):
    """附件引用解析。"""

    def test_relative_attachment_is_normalized(self) -> None:
        """查询串、片段和编码都应去掉，只留规范路径。"""
        self.assertEqual(
            pipeline.attachment_path(
                "data1/20240301/页.html", "./铜月报.PDF?download=1#page=2"
            ),
            "data1/20240301/铜月报.PDF",
        )

    def test_percent_encoded_name_is_decoded(self) -> None:
        """中文名经过 URL 编码后应还原。"""
        self.assertEqual(
            pipeline.attachment_path("data1/20240301/页.html", "%E9%93%9C.pdf"),
            "data1/20240301/铜.pdf",
        )

    def test_remote_and_absolute_are_rejected(self) -> None:
        """远端地址、绝对路径与反斜杠都要报对应错误码。"""
        cases = {
            "attachment_remote": "https://example.com/a.pdf",
            "attachment_empty": "?download=1",
            "attachment_invalid_path": "/etc/passwd",
        }
        for code, href in cases.items():
            with self.subTest(href=href):
                with self.assertRaises(AttachmentPathError) as context:
                    pipeline.attachment_path("data1/20240301/页.html", href)
                self.assertEqual(context.exception.code, code)

    def test_escaping_data_root_is_rejected(self) -> None:
        """爬出资料根的路径必须拒绝。"""
        with self.assertRaises(AttachmentPathError) as context:
            pipeline.attachment_path("data1/20240301/页.html", "../../../secret.pdf")
        self.assertEqual(context.exception.code, "attachment_outside_data")

    def test_inner_relative_attachment_stays(self) -> None:
        """资料根内部向上跳一级仍然合法。"""
        self.assertEqual(
            pipeline.attachment_path("data1/20240301/页.html", "../铜月报.PDF"),
            "data1/铜月报.PDF",
        )


class CollectReportTest(unittest.TestCase):
    """主文与附件汇总。"""

    def setUp(self) -> None:
        """准备配置与替身文章。"""
        self.settings = Settings(
            max_attachment_depth=1,
            max_attachments=2,
        )
        self.article = RawArticle(
            article_id=1,
            relative_path="data1/20240301/页.html",
            content_hash="1" * 64,
            format="html",
            content=b"<html></html>",
        )
        self.attachment = RawArticle(
            article_id=2,
            relative_path="data1/20240301/铜月报.PDF",
            content_hash="2" * 64,
            format="pdf",
            content=b"%PDF-1.7",
        )

    def test_attachment_blocks_and_chain_are_collected(self) -> None:
        """附件块应带引用链登记，缺失附件记问题码。"""
        main_result = ExtractionResult(
            blocks=[make_block("b-0001", "正文")],
            attachment_refs=[
                AttachmentRef(
                    href="./铜月报.PDF",
                    label="下载",
                    location={"format": "html", "element_index": 1},
                ),
                AttachmentRef(href="./缺失.PDF", label="缺失"),
                AttachmentRef(href="https://example.com/x.pdf", label="远端"),
            ],
        )
        attachment_result = ExtractionResult(
            blocks=[
                make_block(
                    "b-0001", "附件正文", article_id=2, kind="paragraph"
                )
            ]
        )

        def fake_extract(article: RawArticle) -> ExtractionResult:
            """按文章 ID 返回预置解析结果。"""
            if article.article_id == 1:
                return main_result
            return attachment_result

        def fake_find(relative_path: str, **_: object) -> int | None:
            """只认识铜月报这一个附件。"""
            return 2 if relative_path.endswith("铜月报.PDF") else None

        def fake_load(article_id: int, **_: object) -> RawArticle:
            """读取预置附件。"""
            return self.attachment

        with (
            mock.patch.object(pipeline, "extract_blocks", fake_extract),
            mock.patch.object(pipeline, "find_article_id", fake_find),
            mock.patch.object(pipeline, "load_article", fake_load),
        ):
            sources = pipeline.collect_report(self.article, self.settings)

        self.assertEqual(len(sources.blocks), 2)
        self.assertEqual([block.order for block in sources.blocks], [1, 2])
        self.assertNotEqual(
            sources.blocks[0].block_id, sources.blocks[1].block_id
        )
        self.assertEqual(sources.blocks[0].block_id, "b-0001")
        self.assertEqual(sources.blocks[1].block_id, "a2-b-0001")
        roles = [part.role for part in sources.parts]
        self.assertEqual(roles.count("main"), 1)
        self.assertEqual(roles.count("attachment"), 2)
        codes = {issue.code for issue in sources.issues}
        self.assertIn("attachment_missing", codes)
        self.assertIn("attachment_remote", codes)
        chain = sources.chains[(2, "2" * 64)]
        self.assertEqual(chain[0]["raw_article_id"], 1)
        self.assertEqual(chain[0]["location"], {"format": "html", "element_index": 1})

    def test_depth_limit_blocks_recursion(self) -> None:
        """超过深度上限时不再解析下一层附件。"""
        main_result = ExtractionResult(
            blocks=[make_block("b-0001", "正文")],
            attachment_refs=[AttachmentRef(href="./铜月报.PDF", label="下载")],
        )
        attachment_result = ExtractionResult(
            blocks=[make_block("b-0001", "附件", article_id=2)],
            attachment_refs=[AttachmentRef(href="./更深.PDF", label="更深")],
        )

        def fake_extract(article: RawArticle) -> ExtractionResult:
            """按文章 ID 返回预置解析结果。"""
            if article.article_id == 1:
                return main_result
            return attachment_result

        with (
            mock.patch.object(pipeline, "extract_blocks", fake_extract),
            mock.patch.object(
                pipeline, "find_article_id", lambda path, **_: 2
            ),
            mock.patch.object(
                pipeline, "load_article", lambda article_id, **_: self.attachment
            ),
        ):
            sources = pipeline.collect_report(self.article, self.settings)

        codes = [issue.code for issue in sources.issues]
        self.assertEqual(codes.count("attachment_depth_exceeded"), 1)


class InputHashTest(unittest.TestCase):
    """输入指纹稳定性。"""

    def test_hash_ignores_part_order(self) -> None:
        """附件顺序不同但内容一致时指纹相同。"""
        first = [
            make_part("main", "a.html", 1, "1" * 64),
            make_part("attachment", "b.pdf", 2, "2" * 64),
        ]
        second = list(reversed(first))
        first[0]["format"] = "html"
        from backend.analysis_store import compute_input_hash

        self.assertEqual(compute_input_hash(first), compute_input_hash(second))

    def test_missing_attachment_changes_hash(self) -> None:
        """附件缺失时 ``content_hash`` 为 null，指纹与已入库时不同。"""
        from backend.analysis_store import compute_input_hash

        present = [
            make_part("main", "a.html", 1, "1" * 64),
            make_part("attachment", "b.pdf", 2, "2" * 64),
        ]
        missing = [
            present[0],
            make_part("attachment", "b.pdf", None, None),
        ]
        self.assertNotEqual(
            compute_input_hash(present), compute_input_hash(missing)
        )


class MergeFallbackTest(unittest.TestCase):
    """兜底状态合并。"""

    def make_scope(self) -> ScopeResult:
        """构造一个规则弃权范围。"""
        return ScopeResult(
            scope_id="cu||short_term",
            product_id="cu",
            contract=None,
            period_key="short_term",
            direction=None,
            statement_type="none",
            review_state="unresolved",
            reason_code="no_explicit_view",
            target_reliable=True,
        )

    def test_inferred_sets_llm_source_and_evidence(self) -> None:
        """有方向时来源改为 llm 并保留模型引文。"""
        scope = self.make_scope()
        evidence = Evidence(
            kind="llm_quote",
            quote="库存下降",
            block_id="b-0001",
            raw_article_id=1,
            content_hash="1" * 64,
        )
        pipeline._merge_fallback(
            scope,
            FallbackResult(
                scope_id=scope.scope_id,
                status="inferred",
                direction="bullish",
                inference_reason="库存下降",
                resolved_evidence=[evidence],
            ),
        )
        self.assertEqual(scope.direction, "bullish")
        self.assertEqual(scope.source, "llm")
        self.assertEqual(scope.review_state, "recognized")
        self.assertEqual(scope.evidence, [evidence])
        self.assertIsNone(scope.reason_code)

    def test_abstained_keeps_unresolved(self) -> None:
        """弃权时保持未明确并记录原因。"""
        scope = self.make_scope()
        pipeline._merge_fallback(
            scope,
            FallbackResult(scope_id=scope.scope_id, status="abstained"),
        )
        self.assertIsNone(scope.direction)
        self.assertEqual(scope.review_state, "unresolved")
        self.assertEqual(scope.reason_code, "model_abstained")

    def test_error_keeps_reason_code(self) -> None:
        """失败时保留具体错误码，便于重试判断。"""
        scope = self.make_scope()
        pipeline._merge_fallback(
            scope,
            FallbackResult(
                scope_id=scope.scope_id,
                status="error",
                error_code="timeout",
                retryable=True,
            ),
        )
        self.assertEqual(scope.reason_code, "model_error:timeout")
        self.assertIsNone(scope.direction)


if __name__ == "__main__":
    unittest.main()
