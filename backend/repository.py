"""原文读取与导入适配层。

阶段 01 的入库逻辑直接复用 ``backend.store``：``read_file`` 负责分块读取、
格式识别与文件名解析，``store_source`` 负责事务写入。``store.load_article``
与 ``store.find_article_id`` 在内部按环境变量建立连接，为了满足“不改 store.py、
仓库中不出现环境变量配置”的要求，本模块用 ``backend.config`` 的连接参数实现
对应只读查询；SQL 与 ``store.py`` 保持一致。

所有函数都接收 ``settings``，方便命令行、API 与测试使用同一套配置。
"""
from __future__ import annotations

import datetime
import hashlib
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from backend.config import Settings, get_settings
from backend.db import open_connection
from backend.errors import ArticleNotFoundError, ConfigurationError
from backend.models import IngestResult, RawArticle

LOGGER = logging.getLogger(__name__)

HEAD_SIZE = 8192
SUPPORTED_SUFFIXES = frozenset({".pdf", ".html", ".htm", ".png"})


def _store_module() -> Any:
    """延迟导入阶段 01 的 ``backend.store``。

    :return: ``backend.store`` 模块。
    :raises ConfigurationError: 仓库中缺少 ``backend/store.py``。
    """
    try:
        from backend import store
    except ImportError as error:  # pragma: no cover - 部署缺少模块时才有意义
        raise ConfigurationError(
            "缺少 backend/store.py，请确认已合并阶段 01 的交付"
        ) from error
    return store


def path_key_of(relative_path: str) -> bytes:
    """计算规范相对路径的 32 字节键。

    :param relative_path: POSIX 风格相对路径。
    :return: SHA-256 原始摘要。
    """
    return hashlib.sha256(relative_path.encode("utf-8")).digest()


def relative_path_of(path: Path, data_root: Path) -> str:
    """取相对资料根的规范路径。

    :param path: 文件路径。
    :param data_root: 资料根目录。
    :return: POSIX 风格相对路径。
    :raises ValueError: 文件不在资料根下。
    """
    return path.resolve().relative_to(data_root.resolve()).as_posix()


def _declared_extension(path: Path) -> str:
    """取小写扩展名，``.htm`` 与 ``.html`` 归一。

    :param path: 文件路径。
    :return: 不含点号的扩展名。
    """
    suffix = path.suffix.lower().removeprefix(".")
    return "html" if suffix == "htm" else suffix


def _extension_mismatch(file_format: str, extension: str) -> bool:
    """判断识别格式与声明扩展名是否不一致。

    :param file_format: 识别出的格式。
    :param extension: 归一后的声明扩展名。
    :return: 两者不匹配时为 ``True``；未知格式不算不匹配。
    """
    return file_format != "unknown" and file_format != extension


def iter_discovered_files(
    data_root: Path | None = None,
) -> Iterator[tuple[Path, str]]:
    """按规范相对路径顺序遍历 ``data1``、``data2`` 下的支持文件。

    :param data_root: 资料根目录，默认取配置。
    :return: ``(绝对路径, 相对路径)`` 迭代器。
    """
    root = data_root or get_settings().data_root
    store = _store_module()
    for path in store.discover_files(root):
        yield path, relative_path_of(path, root)


def probe_file(path: Path, *, data_root: Path) -> dict[str, object]:
    """只读首段探测格式，不计算全文指纹。

    :param path: 文件路径。
    :param data_root: 资料根目录。
    :return: 含相对路径、字节数、格式与问题码的字典。
    """
    store = _store_module()
    with path.open("rb") as stream:
        head = stream.read(HEAD_SIZE)
    file_format, format_issue = store.detect_format(head)
    extension = _declared_extension(path)
    return {
        "relative_path": relative_path_of(path, data_root),
        "byte_size": path.stat().st_size,
        "format": file_format,
        "error_code": format_issue,
        "extension_mismatch": _extension_mismatch(file_format, extension),
    }


def ingest_file(
    path: str | Path,
    *,
    data_root: Path | None = None,
    settings: Settings | None = None,
) -> IngestResult:
    """导入一篇原文。

    读取与格式识别交给 ``store.read_file``，写入交给 ``store.store_source``，
    本函数只负责连接管理与结果类型转换。

    :param path: 文件路径。
    :param data_root: 资料根目录。
    :param settings: 运行配置。
    :return: 含文章 ID、指纹、格式与动作的导入结果。
    """
    active_settings = settings or get_settings()
    root = data_root or active_settings.data_root
    store = _store_module()
    source = store.read_file(Path(path), root)
    with open_connection(active_settings) as connection:
        article_id, status = store.store_source(connection, source)
    LOGGER.info(
        "导入完成：path=%s id=%s status=%s",
        source.relative_path,
        article_id,
        status,
    )
    return IngestResult(
        relative_path=source.relative_path,
        article_id=article_id,
        content_hash=source.content_hash.hex(),
        format=source.file_format,
        status=status,
        error_code=source.format_issue,
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
    for path in paths:
        try:
            yield ingest_file(path, data_root=data_root, settings=settings)
        except Exception as error:  # noqa: BLE001 - 批处理需要继续下一篇
            LOGGER.exception("导入失败：%s", path)
            yield IngestResult(
                relative_path=Path(path).name,
                status="failed",
                error_code=getattr(error, "code", type(error).__name__.lower()),
            )


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
    active_settings = settings or get_settings()
    digest = None
    if content_hash is not None:
        try:
            digest = bytes.fromhex(content_hash)
        except ValueError as error:
            raise ArticleNotFoundError("指纹不是合法十六进制") from error
        if len(content_hash) != 64 or len(digest) != 32:
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

    with open_connection(active_settings, autocommit=True) as connection:
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
        collected_on=_as_date(row["collected_on"]),
        institution_hint=row["institution_hint"],
        format_issue=row["format_issue"],
        extension_mismatch=bool(row["extension_mismatch"]),
    )


def _as_date(value: Any) -> datetime.date | None:
    """把数据库日期列转成 ``datetime.date``。

    :param value: 日期、时间或字符串。
    :return: 日期对象，空值返回 ``None``。
    """
    if value is None or isinstance(value, datetime.date):
        return value
    if isinstance(value, datetime.datetime):
        return value.date()
    return datetime.date.fromisoformat(str(value))


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
    active_settings = settings or get_settings()
    key = path_key_of(relative_path)
    with open_connection(active_settings, autocommit=True) as connection:
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
    active_settings = settings or get_settings()
    statement = "SELECT id FROM raw_articles WHERE id > %s ORDER BY id"
    parameters: tuple[Any, ...] = (after_id,)
    if limit is not None:
        statement += " LIMIT %s"
        parameters += (int(limit),)
    with open_connection(active_settings, autocommit=True) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(statement, parameters)
            rows = cursor.fetchall()
        finally:
            cursor.close()
    return [int(row[0]) for row in rows]
