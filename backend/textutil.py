"""文本与序列化工具。

匹配用的规范化文本、稳定 JSON 和指纹都在这里生成，保证不同模块对同一
输入得到同样结果。
"""
from __future__ import annotations

import datetime
import decimal
import hashlib
import json
import re
import unicodedata
from typing import Any

WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """生成供匹配使用副本：NFKC 归一后合并空白。

    :param text: 原文。
    :return: 全角标点转半角、连续空白压成一个空格的文本。
    """
    return WHITESPACE_PATTERN.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def sha256_hex(data: bytes | str) -> str:
    """计算 64 位小写十六进制 SHA-256。

    :param data: 字节或按 UTF-8 编码的字符串。
    :return: 十六进制指纹。
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def json_ready(value: Any) -> Any:
    """把数据模型递归转换为可 JSON 序列化的值。

    :param value: 任意数据模型、日期或容器。
    :return: 只含 dict／list／str／int／float／bool／None 的结构。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return json_ready(to_dict())
    raise TypeError(f"无法序列化的类型：{type(value)!r}")


def canonical_json(value: Any) -> str:
    """按键名排序序列化，用于计算稳定指纹。

    :param value: 可 JSON 序列化的结构。
    :return: 无多余空白的紧凑 JSON 字符串。
    """
    return json.dumps(
        json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_date(value: str | datetime.date | None) -> datetime.date | None:
    """把 ``YYYY-MM-DD`` 字符串转成日期。

    :param value: 日期或 ISO 字符串，空值原样返回 ``None``。
    :return: ``datetime.date`` 或 ``None``。
    :raises ValueError: 字符串不是合法 ISO 日期。
    """
    if value is None or isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(value)
