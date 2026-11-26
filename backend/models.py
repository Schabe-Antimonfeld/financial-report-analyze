"""跨模块传递的数据模型。

字段名与实施手册的交接 JSON 保持一致：解析器输出 ``Block``，
规则输出 ``RuleResult``，编排输出 ``AnalysisResult``。所有 ``to_dict``
返回可直接 JSON 序列化的结构，``bytes`` 字段（原文内容）不参与交接。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from backend.textutil import json_ready, normalize_text


@dataclass
class Issue:
    """解析或规则阶段的问题记录。"""

    code: str
    page: int | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return {"code": self.code, "page": self.page, "detail": self.detail}


@dataclass
class RawArticle:
    """从 MySQL 读回的一篇原文。"""

    article_id: int
    relative_path: str
    content_hash: str
    format: str
    content: bytes
    collected_on: datetime.date | None = None
    institution_hint: str | None = None
    format_issue: str | None = None
    extension_mismatch: bool = False

    @property
    def byte_size(self) -> int:
        """原始字节数。"""
        return len(self.content)

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        """转为交接 JSON，默认不带原始字节。"""
        data = {
            "article_id": self.article_id,
            "relative_path": self.relative_path,
            "content_hash": self.content_hash,
            "format": self.format,
            "byte_size": self.byte_size,
            "collected_on": self.collected_on,
            "institution_hint": self.institution_hint,
            "format_issue": self.format_issue,
            "extension_mismatch": self.extension_mismatch,
        }
        if include_content:
            data["content_hex"] = self.content.hex()
        return json_ready(data)


@dataclass
class Block:
    """按阅读顺序排列的原文块，附带出处。"""

    block_id: str
    order: int
    raw_article_id: int
    content_hash: str
    kind: str
    text: str
    heading_path: list[str] = field(default_factory=list)
    location: dict[str, Any] = field(default_factory=dict)
    table: dict[str, Any] | None = None
    normalized: str = ""

    def __post_init__(self) -> None:
        """补齐匹配副本与原文字段。"""
        if not self.normalized:
            self.normalized = normalize_text(self.text)

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "block_id": self.block_id,
                "order": self.order,
                "raw_article_id": self.raw_article_id,
                "content_hash": self.content_hash,
                "kind": self.kind,
                "heading_path": self.heading_path,
                "text": self.text,
                "normalized": self.normalized,
                "location": self.location,
                "table": self.table,
            }
        )


@dataclass
class AttachmentRef:
    """HTML 中发现的附件引用。"""

    href: str
    label: str = ""
    location: dict[str, Any] = field(default_factory=dict)
    relative_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "href": self.href,
                "label": self.label,
                "location": self.location,
                "relative_path": self.relative_path,
            }
        )


@dataclass
class ExtractionResult:
    """解析器统一返回值。"""

    blocks: list[Block] = field(default_factory=list)
    attachment_refs: list[AttachmentRef] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "blocks": [block.to_dict() for block in self.blocks],
                "attachment_refs": [ref.to_dict() for ref in self.attachment_refs],
                "issues": [issue.to_dict() for issue in self.issues],
            }
        )


@dataclass
class ReportMetadata:
    """报告级元数据。"""

    institution: str | None = None
    title: str | None = None
    published_on: datetime.date | None = None
    date_source: str | None = None
    collected_on: datetime.date | None = None
    issues: list[Issue] = field(default_factory=list)
    field_sources: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "institution": self.institution,
                "title": self.title,
                "published_on": self.published_on,
                "date_source": self.date_source,
                "collected_on": self.collected_on,
                "issues": [issue.to_dict() for issue in self.issues],
                "field_sources": self.field_sources,
            }
        )


@dataclass
class Evidence:
    """一条可回查的证据。"""

    kind: str
    quote: str
    block_id: str
    raw_article_id: int
    content_hash: str
    location: dict[str, Any] = field(default_factory=dict)
    rule_id: str = ""
    grade: str = ""
    contract: str | None = None
    attachment_chain: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "kind": self.kind,
                "rule_id": self.rule_id,
                "grade": self.grade,
                "quote": self.quote,
                "block_id": self.block_id,
                "raw_article_id": self.raw_article_id,
                "content_hash": self.content_hash,
                "location": self.location,
                "attachment_chain": self.attachment_chain,
                "contract": self.contract,
            }
        )


@dataclass
class ScopeResult:
    """一个（品种, 合约, 周期）范围的观点结果。"""

    scope_id: str
    product_id: str
    contract: str | None
    period_key: str
    period_text: str | None = None
    period_definition: str | None = None
    direction: str | None = None
    source: str | None = None
    statement_type: str = "none"
    review_state: str = "unresolved"
    reason_code: str | None = None
    target_reliable: bool = False
    outlook_text: str | None = None
    conditions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    inference_reason: str | None = None
    fallback_blocks: list[Block] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON，不重复输出兜底原文。"""
        return json_ready(
            {
                "scope_id": self.scope_id,
                "product_id": self.product_id,
                "contract": self.contract,
                "period_key": self.period_key,
                "period_text": self.period_text,
                "period_definition": self.period_definition,
                "direction": self.direction,
                "source": self.source,
                "statement_type": self.statement_type,
                "review_state": self.review_state,
                "reason_code": self.reason_code,
                "target_reliable": self.target_reliable,
                "fallback_blocks": [block.block_id for block in self.fallback_blocks],
                "outlook_text": self.outlook_text,
                "conditions": self.conditions,
                "evidence": [item.to_dict() for item in self.evidence],
                "inference_reason": self.inference_reason,
            }
        )


@dataclass
class RuleResult:
    """规则层对一篇报告的输出。"""

    scopes: list[ScopeResult] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "scopes": [scope.to_dict() for scope in self.scopes],
                "issues": [issue.to_dict() for issue in self.issues],
            }
        )


@dataclass
class FallbackFragment:
    """送入模型的一个片段及其本地来源映射。"""

    request_id: str
    source_block_id: str
    text: str
    block: Block | None = field(default=None, repr=False, compare=False)


@dataclass
class FallbackTask:
    """一个待兜底范围。"""

    scope_id: str
    product_id: str
    contract: str | None
    period_key: str
    fragments: list[FallbackFragment] = field(default_factory=list)
    fragment_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON，不含本地块对象。"""
        return json_ready(
            {
                "scope_id": self.scope_id,
                "product_id": self.product_id,
                "contract": self.contract,
                "period_key": self.period_key,
                "fragment_hash": self.fragment_hash,
                "fragments": [
                    {
                        "block_id": fragment.request_id,
                        "text": fragment.text,
                        "source_block_id": fragment.source_block_id,
                    }
                    for fragment in self.fragments
                ],
            }
        )


@dataclass
class FallbackResult:
    """模型兜底的最终结论。"""

    scope_id: str
    status: str
    direction: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    inference_reason: str | None = None
    error_code: str | None = None
    retryable: bool = False
    resolved_evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "scope_id": self.scope_id,
                "status": self.status,
                "direction": self.direction,
                "evidence": self.evidence,
                "inference_reason": self.inference_reason,
                "error_code": self.error_code,
                "retryable": self.retryable,
            }
        )


@dataclass
class InputPart:
    """参与本次分析的原文版本。"""

    role: str
    relative_path: str
    article_id: int | None
    content_hash: str | None
    format: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "role": self.role,
                "relative_path": self.relative_path,
                "article_id": self.article_id,
                "content_hash": self.content_hash,
                "format": self.format,
            }
        )


@dataclass
class SaveResult:
    """观点写入事务的结果。"""

    status: str
    attempt_id: int | None = None
    report_id: int | None = None
    opinion_count: int = 0
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "status": self.status,
                "attempt_id": self.attempt_id,
                "report_id": self.report_id,
                "opinion_count": self.opinion_count,
                "error_code": self.error_code,
            }
        )


@dataclass
class AnalysisResult:
    """一次分析的完整结果，交给 save_opinions 与 CLI。"""

    article_id: int
    input_hash: str = ""
    analyzer_version: str = ""
    attempt_id: int | None = None
    input_parts: list[InputPart] = field(default_factory=list)
    metadata: ReportMetadata | None = None
    scopes: list[ScopeResult] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    fallback_tasks: list[FallbackTask] = field(default_factory=list)
    extraction_issues: list[Issue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    save_result: SaveResult | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "article_id": self.article_id,
                "attempt_id": self.attempt_id,
                "input_hash": self.input_hash,
                "analyzer_version": self.analyzer_version,
                "input_parts": [part.to_dict() for part in self.input_parts],
                "metadata": self.metadata.to_dict() if self.metadata else None,
                "scopes": [scope.to_dict() for scope in self.scopes],
                "issues": [issue.to_dict() for issue in self.issues],
                "extraction_issues": [
                    issue.to_dict() for issue in self.extraction_issues
                ],
                "fallback_tasks": [task.to_dict() for task in self.fallback_tasks],
                "stats": self.stats,
                "save_result": self.save_result.to_dict() if self.save_result else None,
            }
        )


@dataclass
class IngestResult:
    """单篇导入结果。"""

    relative_path: str
    article_id: int | None = None
    content_hash: str | None = None
    format: str | None = None
    status: str = "failed"
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为交接 JSON。"""
        return json_ready(
            {
                "article_id": self.article_id,
                "relative_path": self.relative_path,
                "content_hash": self.content_hash,
                "format": self.format,
                "status": self.status,
                "error_code": self.error_code,
            }
        )
