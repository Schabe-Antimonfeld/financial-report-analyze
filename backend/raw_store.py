"""原文导入与读取。

对应阶段 01 的交接：``ingest_file``、``load_article``、``find_article_id``。
编排层只通过这里拿 ``RawArticle``，不直接访问文件系统，因此没有 ``data/``
权限的运行环境也能完成分析。
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from backend.config import Settings, get_settings
from backend.db import open_connection
from backend.errors import (
    ArticleNotFoundError,
    DatabaseOperationError,
    STATUS_INSERTED,
    STATUS_SKIPPED,
    STATUS_UPDATED,
)
from backend.models import IngestResult, RawArticle

LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
HEAD_SIZE = 8192
SUPPORTED_SUFFIXES = frozenset({".pdf", ".html", ".htm", ".png"})
FILE_NAME_PATTERN = re.compile(r"(.+?)_([0-9]+)(?:_([0-9]+))?")
PDF_VERSION_PATTERN = re.compile(rb"%PDF-\d\.\d")
HTML_MARKER_PATTERN = re.compile(
    rb"<!doctype\s+html\b|<(?:html|head|body|main|article|div|p)(?:\s|>)",
    re.IGNORECASE,
)
ERROR_PAGE_PATTERN = re.compile(rb"(?:404\b|not found\b)", re.IGNORECASE)
BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)
EXTENSION_GROUPS = {
    "pdf": frozenset({".pdf"}),
    "html": frozenset({".html", ".htm"}),
    "png": frozenset({".png"}),
}


def detect_format(head: bytes) -> tuple[str, str | None]:
    """按字节签名识别文件格式。

    :param head: 文件首段，建议至少 8 KiB。
    :return: ``(格式, 问题码)``，无法识别时格式为 ``unknown``。
    """
    if not head:
        return "unknown", "empty_file"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", None
    if PDF_VERSION_PATTERN.search(head[:1024]):
        return "pdf", None

    prefix = head[:HEAD_SIZE]
    for bom, encoding in BOMS:
        if prefix.startswith(bom):
            prefix = prefix.decode(encoding, errors="ignore").encode("utf-8")
            break
    prefix = prefix.removeprefix(b"\xef\xbb\xbf").lstrip()
    if HTML_MARKER_PATTERN.search(prefix):
        return "html", None
    if ERROR_PAGE_PATTERN.match(prefix):
        return "unknown", "source_error"
    return "unknown", "unknown_signature"


def extension_mismatch(file_format: str, suffix: str) -> bool:
    """判断识别格式与声明扩展名是否不一致。

    :param file_format: 识别出的格式。
    :param suffix: 文件扩展名，含点号。
    :return: 两者不匹配时为 ``True``；``.htm`` 与 ``.html`` 视为等价。
    """
    expected = EXTENSION_GROUPS.get(file_format)
    if expected is None:
        return False
    return suffix.casefold() not in expected


def parse_file_name(stem: str) -> tuple[str | None, str | None, str | None]:
    """拆解文件名中的机构、采集编号与序号。

    :param stem: 不含扩展名的文件名。
    :return: ``(机构, 采集编号, 分部序号)``，缺失项为 ``None``。
    """
    matched = FILE_NAME_PATTERN.fullmatch(stem)
    if matched is None:
        return None, None, None
    return matched.group(1), matched.group(2), matched.group(3)


def parse_collected_on(directory_name: str) -> datetime.date | None:
    """解析采集目录名 ``YYYYMMDD``。

    :param directory_name: 直接父目录名。
    :return: 采集日期，格式不符时为 ``None``。
    """
    try:
        return datetime.datetime.strptime(directory_name, "%Y%m%d").date()
    except ValueError:
        return None


def relative_path_of(path: Path, data_root: Path) -> str:
    """取相对资料根的规范路径。

    :param path: 文件路径。
    :param data_root: 资料根目录。
    :return: POSIX 风格相对路径。
    :raises ValueError: 文件不在资料根下。
    """
    return path.resolve().relative_to(data_root.resolve()).as_posix()


def path_key_of(relative_path: str) -> bytes:
    """计算规范相对路径的 32 字节键。

    :param relative_path: POSIX 风格相对路径。
    :return: SHA-256 原始摘要。
    """
    return hashlib.sha256(relative_path.encode("utf-8")).digest()


def iter_discovered_files(
    data_root: Path | None = None,
) -> Iterator[tuple[Path, str]]:
    """按规范相对路径顺序遍历资料根下的支持文件。

    :param data_root: 资料根目录，默认取配置。
    :return: ``(绝对路径, 相对路径)`` 迭代器。
    """
    root = data_root or get_settings().data_root
    if not root.is_dir():
        raise FileNotFoundError(f"资料根目录不存在：{root}")
    discovered = [
        (item, relative_path_of(item, root))
        for item in root.rglob("*")
        if item.is_file() and item.suffix.casefold() in SUPPORTED_SUFFIXES
    ]
    discovered.sort(key=lambda pair: pair[1])
    return iter(discovered)


def read_file_bytes(path: Path) -> tuple[bytes, str, str | None, bytes]:
    """分块读取文件，同时计算指纹与格式。

    :param path: 文件路径。
    :return: ``(内容, 指纹, 格式, 问题码)``。
    """
    digest = hashlib.sha256()
    buffer = bytearray()
    head = b""
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            buffer.extend(chunk)
            if len(head) < HEAD_SIZE:
                head = bytes(buffer[:HEAD_SIZE])
    content = bytes(buffer)
    file_format, format_issue = detect_format(head)
    return content, digest.hexdigest(), file_format, format_issue


def probe_file(path: Path, *, data_root: Path) -> dict[str, object]:
    """只读首段探测格式，不计算全文指纹。

    :param path: 文件路径。
    :param data_root: 资料根目录。
    :return: 含相对路径、字节数、格式与问题码的字典。
    """
    with path.open("rb") as stream:
        head = stream.read(HEAD_SIZE)
    file_format, format_issue = detect_format(head)
    relative_path = relative_path_of(path, data_root)
    return {
        "relative_path": relative_path,
        "byte_size": path.stat().st_size,
        "format": file_format,
        "error_code": format_issue,
        "extension_mismatch": extension_mismatch(file_format, path.suffix),
    }


def _insert_content(
    connection: Any,
    digest: bytes,
    content: bytes,
    file_format: str,
    format_issue: str | None,
) -> None:
    """登记原文内容，重复哈希时保持原行。

    :param connection: 事务连接。
    :param digest: 32 字节指纹。
    :param content: 原始字节。
    :param file_format: 识别出的格式。
    :param format_issue: 格式问题码。
    """
    statement = """
        INSERT INTO raw_contents
            (content_hash, content, byte_size, file_format, format_issue)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE content_hash = raw_contents.content_hash
    """
    cursor = connection.cursor(prepared=True)
    try:
        cursor.execute(
            statement, (digest, content, len(content), file_format, format_issue)
        )
    finally:
        cursor.close()


def _upsert_article(
    connection: Any,
    *,
    relative_path: str,
    key: bytes,
    digest: bytes,
    suffix: str,
    file_format: str,
    mismatch: bool,
    collected_on: datetime.date | None,
    institution_hint: str | None,
    collect_code: str | None,
    collect_part: str | None,
) -> tuple[int, str]:
    """登记或更新一条采集路径。

    :param connection: 事务连接。
    :return: ``(文章 ID, 动作)``，动作为 ``inserted``／``updated``／``skipped``。
    """
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT id, content_hash, relative_path
            FROM raw_articles WHERE path_key = %s FOR UPDATE
            """,
            (key,),
        )
        existing = cursor.fetchone()
        if existing is None:
            cursor.execute(
                """
                INSERT INTO raw_articles
                    (relative_path, path_key, content_hash, collected_on,
                     institution_hint, collect_code, collect_part,
                     declared_extension, extension_mismatch)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    relative_path,
                    key,
                    digest,
                    collected_on,
                    institution_hint,
                    collect_code,
                    collect_part,
                    suffix.lstrip("."),
                    mismatch,
                ),
            )
            article_id = int(cursor.lastrowid)
            cursor.execute(
                "SELECT id, relative_path FROM raw_articles WHERE id = %s FOR UPDATE",
                (article_id,),
            )
            created = cursor.fetchone()
            if created is None or created["relative_path"] != relative_path:
                raise DatabaseOperationError("并发登记路径后核对失败")
            return article_id, STATUS_INSERTED

        article_id = int(existing["id"])
        if bytes(existing["content_hash"]) == digest:
            return article_id, STATUS_SKIPPED
        cursor.execute(
            """
            UPDATE raw_articles
            SET content_hash = %s, collected_on = %s, institution_hint = %s,
                collect_code = %s, collect_part = %s, declared_extension = %s,
                extension_mismatch = %s
            WHERE id = %s
            """,
            (
                digest,
                collected_on,
                institution_hint,
                collect_code,
                collect_part,
                suffix.lstrip("."),
                mismatch,
                article_id,
            ),
        )
        return article_id, STATUS_UPDATED
    finally:
        cursor.close()


def ingest_file(
    path: str | Path,
    *,
    data_root: Path | None = None,
    settings: Settings | None = None,
) -> IngestResult:
    """导入一篇原文，整篇放在一个事务里。

    :param path: 文件路径。
    :param data_root: 资料根目录。
    :param settings: 运行配置。
    :return: 导入结果，含文章 ID、指纹、格式与动作。
    """
    settings = settings or get_settings()
    root = data_root or settings.data_root
    file_path = Path(path)
    relative_path = relative_path_of(file_path, root)
    key = path_key_of(relative_path)
    content, digest_hex, file_format, format_issue = read_file_bytes(file_path)
    digest = bytes.fromhex(digest_hex)
    suffix = file_path.suffix
    institution_hint, collect_code, collect_part = parse_file_name(file_path.stem)
    collected_on = parse_collected_on(file_path.parent.name)
    mismatch = extension_mismatch(file_format, suffix)

    with open_connection(settings) as connection:
        try:
            connection.start_transaction()
            _insert_content(connection, digest, content, file_format, format_issue)
            article_id, status = _upsert_article(
                connection,
                relative_path=relative_path,
                key=key,
                digest=digest,
                suffix=suffix,
                file_format=file_format,
                mismatch=mismatch,
                collected_on=collected_on,
                institution_hint=institution_hint,
                collect_code=collect_code,
                collect_part=collect_part,
            )
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT IGNORE INTO raw_article_versions
                        (article_id, content_hash)
                    VALUES (%s, %s)
                    """,
                    (article_id, digest),
                )
            finally:
                cursor.close()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    LOGGER.info(
        "导入完成：path=%s id=%s status=%s", relative_path, article_id, status
    )
    return IngestResult(
        relative_path=relative_path,
        article_id=article_id,
        content_hash=digest_hex,
        format=file_format,
        status=status,
        error_code=format_issue,
    )


def ingest_paths(
    paths: Iterable[str | Path],
    *,
    data_root: Path | None = None,
    settings: Settings | None = None,
) -> Iterator[IngestResult]:
    """批量导入，单篇失败不阻断后续文件。

    :param paths: 文件路径集合。
    :param data_root: 资料根目录。
    :param settings: 运行配置。
    :return: 逐篇导入结果。
    """
    settings = settings or get_settings()
    for path in paths:
        try:
            yield ingest_file(path, data_root=data_root, settings=settings)
        except Exception as error:  # noqa: BLE001 - 批处理需要继续下一篇
            LOGGER.exception("导入失败：%s", path)
            relative_path = _safe_relative(path, data_root or settings.data_root)
            yield IngestResult(
                relative_path=relative_path,
                status="failed",
                error_code=getattr(error, "code", type(error).__name__.lower()),
            )


def _safe_relative(path: str | Path, data_root: Path) -> str:
    """尽力取相对路径，失败时退回文件名。"""
    try:
        return relative_path_of(Path(path), data_root)
    except (ValueError, OSError):
        return Path(path).name


def load_article(
    article_id: int,
    content_hash: str | None = None,
    *,
    settings: Settings | None = None,
) -> RawArticle:
    """按文章 ID 读取当前版本或指定指纹版本。

    :param article_id: 文章 ID。
    :param content_hash: 64 位十六进制指纹，``None`` 表示当前版本。
    :param settings: 运行配置。
    :return: 含原始字节的原文。
    :raises ArticleNotFoundError: 文章或指定版本不存在。
    """
    settings = settings or get_settings()
    digest = None
    if content_hash is not None:
        try:
            digest = bytes.fromhex(content_hash)
        except ValueError as error:
            raise ArticleNotFoundError("指纹不是合法十六进制") from error
        if len(digest) != 32:
            raise ArticleNotFoundError("指纹长度不是 32 字节")

    if digest is None:
        statement = """
            SELECT a.id, a.relative_path, LOWER(HEX(a.content_hash)) AS content_hash,
                   c.file_format, c.content, c.format_issue,
                   a.collected_on, a.institution_hint, a.extension_mismatch
            FROM raw_articles AS a
            JOIN raw_contents AS c ON c.content_hash = a.content_hash
            WHERE a.id = %s
        """
        parameters: tuple[Any, ...] = (article_id,)
    else:
        statement = """
            SELECT a.id, a.relative_path, LOWER(HEX(c.content_hash)) AS content_hash,
                   c.file_format, c.content, c.format_issue,
                   a.collected_on, a.institution_hint, a.extension_mismatch
            FROM raw_articles AS a
            JOIN raw_article_versions AS v ON v.article_id = a.id
            JOIN raw_contents AS c ON c.content_hash = v.content_hash
            WHERE a.id = %s AND v.content_hash = %s
        """
        parameters = (article_id, digest)

    with open_connection(settings, autocommit=True) as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(statement, parameters)
            row = cursor.fetchone()
        finally:
            cursor.close()
    if row is None:
        raise ArticleNotFoundError(f"找不到文章：{article_id}")
    return RawArticle(
        article_id=int(row["id"]),
        relative_path=str(row["relative_path"]),
        content_hash=str(row["content_hash"]),
        format=str(row["file_format"]),
        content=bytes(row["content"] or b""),
        collected_on=row["collected_on"],
        institution_hint=row["institution_hint"],
        format_issue=row["format_issue"],
        extension_mismatch=bool(row["extension_mismatch"]),
    )


def find_article_id(
    relative_path: str,
    *,
    settings: Settings | None = None,
) -> int | None:
    """按规范相对路径查文章 ID。

    :param relative_path: POSIX 风格相对路径。
    :param settings: 运行配置。
    :return: 文章 ID，未登记时为 ``None``。
    """
    settings = settings or get_settings()
    key = path_key_of(relative_path)
    with open_connection(settings, autocommit=True) as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id, relative_path FROM raw_articles WHERE path_key = %s",
                (key,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
    if row is None or row["relative_path"] != relative_path:
        return None
    return int(row["id"])


def list_article_ids(
    *,
    limit: int | None = None,
    after_id: int = 0,
    settings: Settings | None = None,
) -> list[int]:
    """按 ID 升序列出已入库文章。

    :param limit: 最大条数，``None`` 表示不限。
    :param after_id: 只取大于该 ID 的文章，用于分批。
    :param settings: 运行配置。
    :return: 文章 ID 列表。
    """
    settings = settings or get_settings()
    statement = "SELECT id FROM raw_articles WHERE id > %s ORDER BY id"
    parameters: tuple[Any, ...] = (after_id,)
    if limit is not None:
        statement += " LIMIT %s"
        parameters += (int(limit),)
    with open_connection(settings, autocommit=True) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(statement, parameters)
            rows = cursor.fetchall()
        finally:
            cursor.close()
    return [int(row[0]) for row in rows]
