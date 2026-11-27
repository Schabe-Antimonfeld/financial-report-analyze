"""兜底规划、SSE 拼接与引文校验测试。"""

from __future__ import annotations

import io
import json
import unittest

from backend.config import FallbackSettings, Settings
from backend.fallback import (
    build_prompt,
    infer_direction,
    plan_fallback,
    read_sse,
    sse_events,
    validate_model_json,
)
from backend.models import Block, RuleResult, ScopeResult


def make_block(
    block_id: str,
    text: str,
    *,
    order: int = 1,
    article_id: int = 1,
) -> Block:
    """构造测试用原文块。"""
    return Block(
        block_id=block_id,
        order=order,
        raw_article_id=article_id,
        content_hash="a" * 64,
        kind="paragraph",
        text=text,
        heading_path=["铜"],
        location={"format": "html", "element_index": order},
    )


def make_scope(blocks: list[Block], **overrides: object) -> ScopeResult:
    """构造待兜底范围。"""
    values: dict[str, object] = {
        "scope_id": "cu||short_term",
        "product_id": "cu",
        "contract": None,
        "period_key": "short_term",
        "direction": None,
        "statement_type": "none",
        "review_state": "unresolved",
        "reason_code": "no_explicit_view",
        "target_reliable": True,
        "fallback_blocks": blocks,
    }
    values.update(overrides)
    return ScopeResult(**values)  # type: ignore[arg-type]


class SseTest(unittest.TestCase):
    """SSE 事件拼接。"""

    def test_two_events_are_concatenated(self) -> None:
        """半行内容跨事件时仍拼成完整 JSON。"""
        left = '{"scope_id":"cu||short_term",'
        right = '"direction":null,"evidence":[],"inference_reason":"资料不足"}'
        start = {"choices": [{"delta": {"content": left}, "finish_reason": None}]}
        stop = {"choices": [{"delta": {"content": right}, "finish_reason": "stop"}]}
        response = (
            "data: " + json.dumps(start) + "\n\n"
            "data: " + json.dumps(stop) + "\n\n"
        )
        result = json.loads(read_sse(io.StringIO(response)))
        self.assertIsNone(result["direction"])

    def test_comment_and_multiline_data_are_supported(self) -> None:
        """注释行忽略，同一事件的多行数据用换行合并。"""
        events = list(sse_events([": keep-alive\n", "data: a\n", "data: b\n", "\n"]))
        self.assertEqual(events, ["a\nb"])

    def test_unfinished_stream_is_rejected(self) -> None:
        """没有 finish_reason=stop 的流按失败处理。"""
        response = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "{}"}, "finish_reason": None}]}
        )
        with self.assertRaises(ValueError) as context:
            read_sse(io.StringIO(response + "\n\n"))
        self.assertEqual(str(context.exception), "stream_incomplete")

    def test_done_marker_is_rejected(self) -> None:
        """``[DONE]`` 不代表 JSON 完整，按失败处理。"""
        with self.assertRaises(ValueError):
            read_sse(io.StringIO("data: [DONE]\n\n"))


class ValidateModelJsonTest(unittest.TestCase):
    """模型 JSON 与引文校验。"""

    def setUp(self) -> None:
        """准备合法结果与片段映射。"""
        self.fragments = {"f-001": "铜现货库存持续下降，现货成交改善。"}
        self.valid = {
            "scope_id": "cu||short_term",
            "direction": "bullish",
            "evidence": [
                {"block_id": "f-001", "quote": "铜现货库存持续下降，现货成交改善。"}
            ],
            "inference_reason": "库存下降且成交改善，推断短期偏强。",
        }

    def test_valid_result_passes(self) -> None:
        """逐字引文通过校验。"""
        result = validate_model_json(
            json.dumps(self.valid, ensure_ascii=False), "cu||short_term", self.fragments
        )
        self.assertEqual(result["direction"], "bullish")

    def test_changed_punctuation_is_invalid(self) -> None:
        """引文只改一个逗号也应判定无效。"""
        broken = json.loads(json.dumps(self.valid, ensure_ascii=False))
        broken["evidence"][0]["quote"] = "铜现货库存持续下降,现货成交改善。"
        with self.assertRaises(ValueError) as context:
            validate_model_json(
                json.dumps(broken, ensure_ascii=False),
                "cu||short_term",
                self.fragments,
            )
        self.assertEqual(str(context.exception), "invalid_evidence")

    def test_scope_mismatch_is_rejected(self) -> None:
        """范围编号必须完全一致。"""
        with self.assertRaises(ValueError) as context:
            validate_model_json(
                json.dumps(self.valid, ensure_ascii=False),
                "cu||medium_term",
                self.fragments,
            )
        self.assertEqual(str(context.exception), "scope_mismatch")

    def test_direction_requires_evidence(self) -> None:
        """有方向却没有引文视为无效。"""
        broken = dict(self.valid, evidence=[])
        with self.assertRaises(ValueError):
            validate_model_json(
                json.dumps(broken, ensure_ascii=False),
                "cu||short_term",
                self.fragments,
            )

    def test_abstained_with_empty_evidence_passes(self) -> None:
        """弃权时允许空证据。"""
        abstained = dict(
            self.valid,
            direction=None,
            evidence=[],
            inference_reason="资料不足",
        )
        result = validate_model_json(
            json.dumps(abstained, ensure_ascii=False),
            "cu||short_term",
            self.fragments,
        )
        self.assertIsNone(result["direction"])


class PlanFallbackTest(unittest.TestCase):
    """兜底准入条件与任务构造。"""

    def test_candidate_needs_all_conditions(self) -> None:
        """有条件观点、冲突或目标不可靠都不得进入兜底。"""
        blocks = [make_block("b-0001", "铜现货库存下降。")]
        self.assertEqual(len(plan_fallback(RuleResult([make_scope(blocks)]))), 1)
        for overrides in (
            {"statement_type": "conditional"},
            {"reason_code": "direction_conflict"},
            {"target_reliable": False},
            {"direction": "bullish"},
            {"fallback_blocks": []},
        ):
            with self.subTest(overrides=overrides):
                scope = make_scope(blocks, **overrides)
                self.assertEqual(plan_fallback(RuleResult([scope])), [])

    def test_fragments_are_limited_and_ordered(self) -> None:
        """最多三条，按原文顺序发送，请求编号从 f-001 开始。"""
        blocks = [
            make_block(f"b-{index:04d}", f"铜分析第{index}段。", order=index)
            for index in range(1, 6)
        ]
        tasks = plan_fallback(RuleResult([make_scope(blocks)]), max_fragments=3)
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(
            [fragment.request_id for fragment in task.fragments],
            ["f-001", "f-002", "f-003"],
        )
        self.assertEqual(
            [fragment.block.order for fragment in task.fragments], [1, 2, 3]
        )
        self.assertEqual(len(task.fragment_hash), 64)

    def test_fragment_hash_depends_on_text(self) -> None:
        """片段原文变化时缓存键随之变化。"""
        first = plan_fallback(
            RuleResult([make_scope([make_block("b-0001", "铜库存下降。")])])
        )[0]
        second = plan_fallback(
            RuleResult([make_scope([make_block("b-0001", "铜库存上升。")])])
        )[0]
        self.assertNotEqual(first.fragment_hash, second.fragment_hash)

    def test_prompt_contains_only_scope_and_fragments(self) -> None:
        """提示只带范围与片段，不带页码坐标或正文全文。"""
        task = plan_fallback(
            RuleResult([make_scope([make_block("b-0001", "铜库存下降。")])])
        )[0]
        prompt = build_prompt(task)
        self.assertIn('"scope_id": "cu||short_term"', prompt)
        self.assertIn("铜库存下降。", prompt)
        self.assertNotIn("element_index", prompt)


class InferDirectionTest(unittest.TestCase):
    """模型调用与结果映射。"""

    def setUp(self) -> None:
        """构造任务。"""
        self.task = plan_fallback(
            RuleResult(
                [make_scope([make_block("b-0001", "铜现货库存持续下降，现货成交改善。")])]
            )
        )[0]

    def test_inferred_result_maps_local_evidence(self) -> None:
        """通过校验后证据应带回原块号与位置。"""
        response = json.dumps(
            {
                "scope_id": "cu||short_term",
                "direction": "bullish",
                "evidence": [
                    {"block_id": "f-001", "quote": "铜现货库存持续下降，现货成交改善。"}
                ],
                "inference_reason": "库存下降且成交改善，推断短期偏强。",
            },
            ensure_ascii=False,
        )
        result = infer_direction(self.task, completion=lambda _prompt: response)
        self.assertEqual(result.status, "inferred")
        self.assertEqual(result.direction, "bullish")
        self.assertEqual(result.resolved_evidence[0].block_id, "b-0001")
        self.assertEqual(result.resolved_evidence[0].kind, "llm_quote")

    def test_invalid_evidence_is_reported(self) -> None:
        """伪造引文返回 invalid 且不产生方向。"""
        response = json.dumps(
            {
                "scope_id": "cu||short_term",
                "direction": "bullish",
                "evidence": [{"block_id": "f-001", "quote": "不存在的句子"}],
                "inference_reason": "推断",
            },
            ensure_ascii=False,
        )
        result = infer_direction(self.task, completion=lambda _prompt: response)
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.error_code, "invalid_evidence")
        self.assertIsNone(result.direction)
        self.assertFalse(result.retryable)

    def test_completion_stub_ignores_disabled_flag(self) -> None:
        """离线替身不受 fallback.enabled 开关影响。"""
        response = json.dumps(
            {
                "scope_id": "cu||short_term",
                "direction": "bullish",
                "evidence": [
                    {"block_id": "f-001", "quote": "铜现货库存持续下降，现货成交改善。"}
                ],
                "inference_reason": "库存下降，推断短期偏强。",
            },
            ensure_ascii=False,
        )
        settings = Settings(fallback=FallbackSettings(enabled=False))
        result = infer_direction(
            self.task, settings=settings, completion=lambda _prompt: response
        )
        self.assertEqual(result.status, "inferred")
        self.assertEqual(result.direction, "bullish")

    def test_timeout_is_retryable(self) -> None:
        """超时标记可重试，规则结果仍可提交。"""

        def raise_timeout(_prompt: str) -> str:
            """模拟读取超时。"""
            raise TimeoutError("read timed out")

        result = infer_direction(self.task, completion=raise_timeout)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "timeout")
        self.assertTrue(result.retryable)


if __name__ == "__main__":
    unittest.main()
