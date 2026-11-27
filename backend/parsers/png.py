"""PNG 图片 OCR 解析。

先校验 PNG 签名与 IHDR，再调用 Tesseract 的 TSV 输出，按文本行聚合词和
像素坐标；低置信度词只记录复核问题，不丢弃识别文本。
"""
from __future__ import annotations

import csv
import io
import struct
import subprocess
from typing import Any

from backend.models import Block, ExtractionResult, Issue, RawArticle

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TESSERACT_COMMAND = [
    "tesseract",
    "stdin",
    "stdout",
    "-l",
    "chi_sim+eng",
    "--psm",
    "3",
    "tsv",
]
LOW_CONFIDENCE = 60.0


def _is_ascii_word(text: str) -> bool:
    """判断词是否为含字母或数字的 ASCII 词。

    :param text: 单个词文本。
    :return: 是 ASCII 且至少含一个字母或数字时为 ``True``。
    """
    return bool(text) and text.isascii() and any(char.isalnum() for char in text)


def _join_words(words: list[str]) -> str:
    """按中英文混排规则拼接 OCR 词。

    :param words: 同一行的词。
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


def _stdout_bytes(completed: subprocess.CompletedProcess[Any]) -> bytes:
    """把子进程标准输出统一转成字节。

    :param completed: 子进程结果。
    :return: 标准输出字节。
    """
    output = completed.stdout
    if output is None:
        return b""
    if isinstance(output, bytes):
        return output
    return str(output).encode("utf-8")


def _int_field(row: dict[str, str | None], name: str) -> int:
    """读取 TSV 中的整数字段。

    :param row: 一行 TSV。
    :param name: 字段名。
    :return: 整数值；缺失或非法时返回 0。
    """
    raw = (row.get(name) or "").strip()
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _confidence(row: dict[str, str | None]) -> float:
    """读取词置信度。

    :param row: 一行 TSV。
    :return: 置信度；缺失或非法时视为高置信度。
    """
    raw = (row.get("conf") or "").strip()
    try:
        return float(raw)
    except ValueError:
        return 100.0


def _new_block(
    article: RawArticle,
    text: str,
    bbox: list[int],
    image_size: list[int],
) -> Block:
    """构造 PNG 文本块。

    :param article: 当前原文。
    :param text: 行文本。
    :param bbox: 行矩形。
    :param image_size: 图片宽高。
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
        location={
            "format": "png",
            "bbox": bbox,
            "image_size": image_size,
        },
    )


def _parse_ocr_tsv(
    output: bytes,
    article: RawArticle,
    image_size: list[int],
) -> tuple[list[Block], list[Issue]]:
    """把 Tesseract TSV 聚合为逐行块。

    :param output: Tesseract 标准输出。
    :param article: 当前原文。
    :param image_size: 图片宽高。
    :return: 正文块和低置信度问题。
    """
    text = output.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    groups: dict[tuple[int, int, int, int], list[tuple[str, int, int, int, int, float]]] = {}
    for row in reader:
        if (row.get("level") or "").strip() != "5":
            continue
        word = (row.get("text") or "").strip()
        if not word:
            continue
        key = (
            _int_field(row, "page_num"),
            _int_field(row, "block_num"),
            _int_field(row, "par_num"),
            _int_field(row, "line_num"),
        )
        left = _int_field(row, "left")
        top = _int_field(row, "top")
        width = _int_field(row, "width")
        height = _int_field(row, "height")
        groups.setdefault(key, []).append(
            (word, left, top, left + width, top + height, _confidence(row))
        )

    blocks: list[Block] = []
    issues: list[Issue] = []
    for key, words in groups.items():
        text_line = _join_words([word[0] for word in words])
        if not text_line:
            continue
        bbox = [
            min(word[1] for word in words),
            min(word[2] for word in words),
            max(word[3] for word in words),
            max(word[4] for word in words),
        ]
        blocks.append(_new_block(article, text_line, bbox, list(image_size)))
        low_words = [word[0] for word in words if word[5] < LOW_CONFIDENCE]
        if low_words:
            issues.append(
                Issue(
                    "ocr_review",
                    key[0],
                    "OCR 低置信度词：" + "、".join(low_words),
                )
            )
    return blocks, issues


def _command_issue(error: BaseException) -> Issue:
    """把 OCR 子进程异常映射为问题码。

    :param error: 子进程异常。
    :return: 问题记录。
    """
    if isinstance(error, FileNotFoundError):
        return Issue(
            "ocr_tool_missing",
            None,
            "未找到 tesseract，请安装 Tesseract OCR",
        )
    if isinstance(error, subprocess.TimeoutExpired):
        return Issue("ocr_timeout", None, "OCR 超时：tesseract 未在限定时间内完成")
    return Issue("ocr_failed", None, "OCR 失败：tesseract 返回非零状态")


def extract_png(article: RawArticle, *, timeout: float = 60) -> ExtractionResult:
    """提取 PNG 图片中的文本行。

    :param article: 含 PNG 字节的原文。
    :param timeout: OCR 子进程超时秒数。
    :return: 正文块与问题列表；可预期失败不会向外抛出异常。
    """
    content = article.content
    if (
        len(content) < 24
        or not content.startswith(PNG_SIGNATURE)
        or content[12:16] != b"IHDR"
    ):
        return ExtractionResult(
            issues=[Issue("png_corrupt", None, "PNG 签名或 IHDR 数据无效")]
        )
    width, height = struct.unpack(">II", content[16:24])
    try:
        completed = subprocess.run(
            TESSERACT_COMMAND,
            input=content,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return ExtractionResult(issues=[_command_issue(error)])
    blocks, issues = _parse_ocr_tsv(
        _stdout_bytes(completed),
        article,
        [width, height],
    )
    return ExtractionResult(blocks=blocks, issues=issues)
