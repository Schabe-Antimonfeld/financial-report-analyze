"""解析器注册与格式分流。

各格式的解析函数由 2、3 号按统一签名交付：``extract_pdf``、``extract_png``、
``extract_html``。本模块只负责按 ``article.format`` 选函数、兜住解析器缺失，
并统一补齐 ``normalized``、排序等公共字段，正文与附件都走这里。
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable

from backend.models import Block, ExtractionResult, Issue, RawArticle
from backend.textutil import normalize_text

LOGGER = logging.getLogger(__name__)

# 格式到「模块名, 函数名」的映射；模块缺失时按 parser_unavailable 处理。
PARSER_REGISTRY: dict[str, tuple[str, str]] = {
    "pdf": ("backend.parsers.pdf", "extract_pdf"),
    "html": ("backend.parsers.html", "extract_html"),
    "png": ("backend.parsers.png", "extract_png"),
}

Parser = Callable[[RawArticle], ExtractionResult]


def load_parser(file_format: str) -> Parser | None:
    """按格式导入解析函数。

    :param file_format: ``pdf``、``html`` 或 ``png``。
    :return: 解析函数；格式未知或模块缺失时返回 ``None``。
    """
    target = PARSER_REGISTRY.get(file_format)
    if target is None:
        return None
    module_name, function_name = target
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        LOGGER.warning("解析模块缺失：%s", module_name, exc_info=True)
        return None
    parser = getattr(module, function_name, None)
    return parser if callable(parser) else None


def _finalize_blocks(blocks: list[Block], article: RawArticle) -> list[Block]:
    """补齐公共字段并保证阅读顺序稳定。

    :param blocks: 解析器产出的块。
    :param article: 对应原文。
    :return: 可直接交给规则层的块列表。
    """
    normalized_blocks: list[Block] = []
    for order, block in enumerate(blocks, start=1):
        block.order = order
        if not block.block_id:
            block.block_id = f"b-{order:04d}"
        if not block.raw_article_id:
            block.raw_article_id = article.article_id
        if not block.content_hash:
            block.content_hash = article.content_hash
        if not block.normalized:
            block.normalized = normalize_text(block.text)
        if block.heading_path is None:
            block.heading_path = []
        normalized_blocks.append(block)
    return normalized_blocks


def extract_blocks(article: RawArticle) -> ExtractionResult:
    """按实际文件格式提取文本块。

    :param article: 已入库原文，含字节与来源版本。
    :return: ``blocks``、``attachment_refs``、``issues`` 三元组；局部失败时仍
        返回可读块和问题列表。
    """
    parser = load_parser(article.format)
    if parser is None:
        detail = (
            f"未注册 {article.format} 格式解析器"
            if article.format in PARSER_REGISTRY
            else f"未知文件格式：{article.format}"
        )
        return ExtractionResult(issues=[Issue("parser_unavailable", None, detail)])

    try:
        result = parser(article)
    except Exception as error:  # noqa: BLE001 - 解析器失败要转成问题码
        LOGGER.exception(
            "解析失败：article_id=%s format=%s", article.article_id, article.format
        )
        return ExtractionResult(
            issues=[Issue("parse_error", None, f"{type(error).__name__}: {error}")]
        )

    if not isinstance(result, ExtractionResult):
        return ExtractionResult(
            issues=[Issue("parse_error", None, "解析器返回类型不是 ExtractionResult")]
        )
    result.blocks = _finalize_blocks(result.blocks, article)
    return result
