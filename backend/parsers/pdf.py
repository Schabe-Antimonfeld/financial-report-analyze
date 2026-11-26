"""PDF 正文解析。

优先使用 Poppler ``pdftotext`` 的 bbox-layout XHTML 提取带坐标的文本行；
版面解析失败时回退到纯文本模式，保证仍能返回可读正文。
"""
from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from typing import Any

from backend.models import Block, ExtractionResult, Issue, RawArticle

PDF_LAYOUT_COMMAND = ["pdftotext", "-bbox-layout", "-enc", "UTF-8", "-", "-"]
PDF_TEXT_COMMAND = ["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"]
WHITESPACE_PATTERN = re.compile(r"\s+")


def _local_name(tag: Any) -> str:
    """取得忽略命名空间后的元素名。

    :param tag: ElementTree 元素的标签。
    :return: 不含 ``{namespace}`` 前缀的标签名。
    """
    text = str(tag)
    if "}" in text:
        return text.rsplit("}", 1)[1]
    return text


def _is_ascii_word(text: str) -> bool:
    """判断词是否为含字母或数字的 ASCII 词。

    :param text: 单个词文本。
    :return: 是 ASCII 且至少含一个字母或数字时为 ``True``。
    """
    return bool(text) and text.isascii() and any(char.isalnum() for char in text)


def _join_words(words: list[str]) -> str:
    """按中英文混排规则拼接词。

    只有相邻两个词都是 ASCII 词时才补一个空格，中文词之间不补空格。

    :param words: 同一行内的词文本列表。
    :return: 拼接后的行文本。
    """
    pieces: list[str] = []
    previous = ""
    for word in words:
        if not word:
            continue
        if pieces and _is_ascii_word(previous) and _is_ascii_word(word):
            pieces.append(" ")
        pieces.append(word)
        previous = word
    return "".join(pieces)


def _number_attr(element: ET.Element, name: str) -> float | None:
    """读取浮点属性。

    :param element: XHTML 元素。
    :param name: 属性名。
    :return: 属性值；缺失或非法时返回 ``None``。
    """
    value = element.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _word_bbox(word: ET.Element) -> list[float] | None:
    """读取单个词的矩形。

    :param word: ``word`` 元素。
    :return: ``[x_min, y_min, x_max, y_max]`` 或 ``None``。
    """
    x_min = _number_attr(word, "xMin")
    y_min = _number_attr(word, "yMin")
    x_max = _number_attr(word, "xMax")
    y_max = _number_attr(word, "yMax")
    if None in (x_min, y_min, x_max, y_max):
        return None
    return [x_min, y_min, x_max, y_max]


def _line_bbox(word_boxes: list[list[float]]) -> list[float] | None:
    """合并同一行内所有词的矩形。

    :param word_boxes: 词矩形列表。
    :return: 行矩形；无有效词矩形时返回 ``None``。
    """
    if not word_boxes:
        return None
    x_min = min(box[0] for box in word_boxes)
    y_min = min(box[1] for box in word_boxes)
    x_max = max(box[2] for box in word_boxes)
    y_max = max(box[3] for box in word_boxes)
    return [x_min, y_min, x_max, y_max]


def _new_block(
    article: RawArticle,
    text: str,
    location: dict[str, Any],
) -> Block:
    """构造解析器内部块，公共字段留给调度层补齐。

    :param article: 当前原文。
    :param text: 块文本。
    :param location: 出处坐标。
    :return: 正文块。
    """
    return Block(
        block_id="",
        order=0,
        raw_article_id=article.article_id,
        content_hash=article.content_hash,
        kind="paragraph",
        text=text,
        heading_path=[],
        location=location,
    )


def _parse_layout_xhtml(root: ET.Element, article: RawArticle) -> list[Block]:
    """从 bbox-layout XHTML 中逐行提取文本。

    :param root: XHTML 根元素。
    :param article: 当前原文。
    :return: 按文档顺序排列的正文块。
    """
    blocks: list[Block] = []
    page_number = 0
    page_size: list[float | None] = [None, None]
    for element in root.iter():
        tag = _local_name(element.tag)
        if tag == "page":
            page_number += 1
            page_size = [
                _number_attr(element, "width"),
                _number_attr(element, "height"),
            ]
            continue
        if tag != "line":
            continue

        words: list[str] = []
        word_boxes: list[list[float]] = []
        for descendant in element.iter():
            if _local_name(descendant.tag) != "word":
                continue
            word_text = "".join(descendant.itertext()).strip()
            if word_text:
                words.append(word_text)
            box = _word_bbox(descendant)
            if box is not None:
                word_boxes.append(box)

        text = _join_words(words)
        if not text:
            text = WHITESPACE_PATTERN.sub(" ", "".join(element.itertext())).strip()
        if not text:
            continue
        location: dict[str, Any] = {
            "format": "pdf",
            "page": page_number or 1,
            "bbox": _line_bbox(word_boxes),
            "page_size": list(page_size),
        }
        blocks.append(_new_block(article, text, location))
    return blocks


def _stdout_bytes(completed: subprocess.CompletedProcess[Any]) -> bytes:
    """把子进程标准输出统一转成字节。

    :param completed: 子进程结果。
    :return: 标准输出字节；缺失时为空字节。
    """
    output = completed.stdout
    if output is None:
        return b""
    if isinstance(output, bytes):
        return output
    return str(output).encode("utf-8")


def _parse_text_pages(output: bytes, article: RawArticle) -> list[Block]:
    """解析纯文本回退结果，按换页符分页。

    :param output: ``pdftotext -layout`` 的标准输出。
    :param article: 当前原文。
    :return: 不带坐标的正文块。
    """
    blocks: list[Block] = []
    text = output.decode("utf-8", errors="replace")
    for page_number, page_text in enumerate(text.split("\f"), start=1):
        for raw_line in page_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            location = {
                "format": "pdf",
                "page": page_number,
                "bbox": None,
                "page_size": None,
            }
            blocks.append(_new_block(article, line, location))
    return blocks


def _command_issue(error: BaseException, *, fallback: bool = False) -> Issue:
    """把子进程异常映射为问题码。

    :param error: 子进程抛出的异常。
    :param fallback: 是否发生在纯文本回退阶段。
    :return: 对应的问题记录。
    """
    prefix = "纯文本回退" if fallback else "PDF 版面提取"
    if isinstance(error, FileNotFoundError):
        return Issue(
            "pdf_tool_missing",
            None,
            f"{prefix}失败：未找到 pdftotext，请安装 Poppler",
        )
    if isinstance(error, subprocess.TimeoutExpired):
        return Issue("pdf_timeout", None, f"{prefix}超时：pdftotext 未在限定时间内完成")
    return Issue("pdf_command_failed", None, f"{prefix}失败：pdftotext 返回非零状态")


def _run_fallback(
    article: RawArticle,
    timeout: float,
    issues: list[Issue],
) -> ExtractionResult:
    """执行纯文本回退并返回结果。

    :param article: 当前原文。
    :param timeout: 子进程超时秒数。
    :param issues: 已记录的问题，会追加回退问题。
    :return: 回退得到的解析结果。
    """
    issues.append(
        Issue("pdf_text_fallback", None, "版面解析失败，已回退到纯文本提取")
    )
    try:
        completed = subprocess.run(
            PDF_TEXT_COMMAND,
            input=article.content,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        issues.append(_command_issue(error, fallback=True))
        return ExtractionResult(blocks=[], issues=issues)
    blocks = _parse_text_pages(_stdout_bytes(completed), article)
    if not blocks:
        issues.append(Issue("pdf_text_empty", None, "PDF 没有可读取的文字层"))
    return ExtractionResult(blocks=blocks, issues=issues)


def extract_pdf(article: RawArticle, *, timeout: float = 60) -> ExtractionResult:
    """提取 PDF 正文文本块。

    :param article: 含 PDF 字节的原文。
    :param timeout: 单次 ``pdftotext`` 调用的超时秒数。
    :return: 正文块与问题列表；可预期失败不会向外抛出异常。
    """
    issues: list[Issue] = []
    try:
        completed = subprocess.run(
            PDF_LAYOUT_COMMAND,
            input=article.content,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        issues.append(_command_issue(error))
        return _run_fallback(article, timeout, issues)

    try:
        root = ET.fromstring(_stdout_bytes(completed))
        blocks = _parse_layout_xhtml(root, article)
    except (ET.ParseError, TypeError, ValueError):
        issues.append(
            Issue("pdf_layout_failed", None, "PDF 版面解析失败：版面 XML 无效")
        )
        return _run_fallback(article, timeout, issues)

    if not blocks:
        issues.append(Issue("pdf_text_empty", None, "PDF 没有可读取的文字层"))
    return ExtractionResult(blocks=blocks, issues=issues)
