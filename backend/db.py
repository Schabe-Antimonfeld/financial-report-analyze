"""MySQL 连接、事务与重试工具。

驱动缺失或连接失败统一转成 ``DatabaseUnavailableError``，让 API 返回 503、
CLI 打印可读原因，而不是把驱动堆栈直接抛给调用方。
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from backend.config import DatabaseSettings, Settings, get_settings
from backend.errors import (
    ConfigurationError,
    DatabaseOperationError,
    DatabaseUnavailableError,
)

LOGGER = logging.getLogger(__name__)

# InnoDB 死锁与锁等待超时；连接中断也按可重试处理。
RETRYABLE_ERRNOS = frozenset({1205, 1213, 2006, 2013})

T = TypeVar("T")


def _load_driver() -> Any:
    """延迟导入 MySQL Connector/Python。

    :return: ``mysql.connector`` 模块。
    :raises DatabaseUnavailableError: 驱动未安装。
    """
    try:
        import mysql.connector
    except ImportError as error:  # pragma: no cover - 依赖缺失时才有意义
        raise DatabaseUnavailableError(
            "未安装 mysql-connector-python，请先执行 pip install -r requirements.txt"
        ) from error
    return mysql.connector


def connect(
    settings: DatabaseSettings | Settings | None = None,
    *,
    database: str | None = None,
    autocommit: bool = False,
) -> Any:
    """建立一条新的 MySQL 连接。

    :param settings: 数据库配置或全局配置，默认读取环境变量。
    :param database: 覆盖库名，``None`` 时使用配置值。
    :param autocommit: 是否自动提交，查询连接可设为 ``True``。
    :return: 连接对象。
    :raises ConfigurationError: 未提供数据库密码。
    :raises DatabaseUnavailableError: 驱动缺失或连接失败。
    """
    if settings is None:
        database_settings: DatabaseSettings = get_settings().database
    elif isinstance(settings, Settings):
        database_settings = settings.database
    else:
        database_settings = settings
    if not database_settings.is_password_set:
        raise ConfigurationError("未设置 DB_PASSWORD 环境变量")

    driver = _load_driver()
    try:
        return driver.connect(
            host=database_settings.host,
            port=database_settings.port,
            user=database_settings.user,
            password=database_settings.password,
            database=database or database_settings.name,
            charset="utf8mb4",
            use_pure=True,
            autocommit=autocommit,
            connection_timeout=database_settings.connect_timeout,
            consume_results=True,
        )
    except driver.Error as error:
        raise DatabaseUnavailableError(f"连接 MySQL 失败：{error}") from error


@contextlib.contextmanager
def open_connection(
    settings: DatabaseSettings | Settings | None = None,
    *,
    autocommit: bool = False,
) -> Iterator[Any]:
    """打开并在退出时关闭一条连接。

    :param settings: 数据库配置或全局配置。
    :param autocommit: 是否自动提交。
    :return: 连接上下文。
    """
    connection = connect(settings, autocommit=autocommit)
    try:
        yield connection
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - 关闭失败不应掩盖原始异常
            LOGGER.debug("关闭数据库连接失败", exc_info=True)


@contextlib.contextmanager
def transaction(
    connection: Any,
    *,
    dictionary: bool = True,
) -> Iterator[Any]:
    """在一个显式事务里提供游标。

    :param connection: 已建立的连接。
    :param dictionary: 是否用字典游标。
    :return: 游标上下文。
    """
    cursor = connection.cursor(dictionary=dictionary)
    try:
        connection.start_transaction()
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def error_number(error: BaseException) -> int | None:
    """取 MySQL 错误号。

    :param error: 驱动异常。
    :return: 错误号，取不到时为 ``None``。
    """
    return getattr(error, "errno", None)


def is_retryable(error: BaseException) -> bool:
    """判断异常是否值得整笔事务重试。

    :param error: 待判断异常。
    :return: 死锁、锁等待超时或连接中断时为 ``True``。
    """
    return error_number(error) in RETRYABLE_ERRNOS


def execute_in_transaction(
    work: Callable[[Any], T],
    settings: DatabaseSettings | Settings | None = None,
    *,
    retries: int = 2,
) -> T:
    """在一个事务里执行 work，死锁时按上限重试。

    :param work: 接收连接并完成整笔事务的函数。
    :param settings: 数据库配置。
    :param retries: 首次失败后的额外重试次数。
    :return: work 的返回值。
    :raises DatabaseOperationError: 重试后仍失败。
    """
    attempts = retries + 1
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        with open_connection(settings) as connection:
            try:
                return work(connection)
            except Exception as error:  # noqa: BLE001 - 需要按错误号分流
                last_error = error
                if not is_retryable(error) or attempt == attempts:
                    break
                LOGGER.warning(
                    "事务因锁冲突失败，第 %s 次重试：%s", attempt, error
                )
    if isinstance(last_error, Exception):
        raise DatabaseOperationError(f"事务执行失败：{last_error}") from last_error
    raise DatabaseOperationError("事务执行失败")
