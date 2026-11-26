"""运行配置。

配置从仓库根目录的 ``config.local.json`` 读取，模板见 ``config.example.json``。
``config.local.json`` 不提交到仓库，因此 GitHub 上不会出现数据库密码或模型令牌；
本模块也不读取任何环境变量。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.errors import ConfigurationError

CONFIG_FILENAME = "config.local.json"
EXAMPLE_FILENAME = "config.example.json"


@dataclass(frozen=True)
class DatabaseSettings:
    """MySQL 连接参数。"""

    host: str = "127.0.0.1"
    port: int = 3306
    name: str = "futures_reports"
    user: str = "root"
    password: str = ""
    connect_timeout: int = 10

    @property
    def is_password_set(self) -> bool:
        """是否已在本地配置中填写密码。"""
        return bool(self.password)


@dataclass(frozen=True)
class FallbackSettings:
    """模型兜底接口配置。"""

    endpoint: str = ""
    api_token: str = ""
    model_id: str = "course-fallback"
    prompt_version: str = "fallback-v1"
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    max_response_bytes: int = 262144
    max_fragments: int = 3
    enabled: bool = True


@dataclass(frozen=True)
class Settings:
    """一次进程运行使用的全部配置。"""

    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    fallback: FallbackSettings = field(default_factory=FallbackSettings)
    data_root: Path = Path("data")
    analyzer_version: str = "backend-1.0.0"
    parser_timeout_seconds: int = 60
    max_attachment_depth: int = 2
    max_attachments: int = 8
    analyze_workers: int = 1
    api_page_size: int = 50
    api_max_page_size: int = 100
    api_cors_origins: tuple[str, ...] = ()

    def attachment_limits(self) -> tuple[int, int]:
        """返回附件深度与数量的上限。

        :return: ``(最大递归深度, 单篇最大附件数)``。
        """
        return self.max_attachment_depth, self.max_attachments


def repository_root() -> Path:
    """返回仓库根目录。

    :return: ``backend`` 的上一级目录。
    """
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    """返回默认的本地配置文件路径。

    :return: 仓库根目录下的 ``config.local.json``。
    """
    return repository_root() / CONFIG_FILENAME


def example_config_path() -> Path:
    """返回配置模板路径。

    :return: 仓库根目录下的 ``config.example.json``。
    """
    return repository_root() / EXAMPLE_FILENAME


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """读取一个对象型配置段。

    :param raw: 顶层配置。
    :param key: 段名。
    :return: 该段的字典，缺省为空字典。
    :raises ConfigurationError: 该段不是对象。
    """
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"配置项 {key} 必须是 JSON 对象")
    return value


def _text(section: dict[str, Any], key: str, default: str = "") -> str:
    """读取字符串配置。

    :param section: 配置段。
    :param key: 键名。
    :param default: 缺省值。
    :return: 字符串值。
    :raises ConfigurationError: 值的类型不是字符串。
    """
    value = section.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigurationError(f"配置项 {key} 必须是字符串")
    return value


def _integer(
    section: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
) -> int:
    """读取整数配置。

    :param section: 配置段。
    :param key: 键名。
    :param default: 缺省值。
    :param minimum: 允许的最小值。
    :return: 整数值。
    :raises ConfigurationError: 值的类型或范围不合法。
    """
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value)
        else:
            raise ConfigurationError(f"配置项 {key} 必须是整数")
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"配置项 {key} 不能小于 {minimum}")
    return value


def _number(
    section: dict[str, Any],
    key: str,
    default: float,
) -> float:
    """读取浮点配置。

    :param section: 配置段。
    :param key: 键名。
    :param default: 缺省值。
    :return: 浮点值。
    :raises ConfigurationError: 值的类型不是数字。
    """
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"配置项 {key} 必须是数字")
    return float(value)


def _boolean(section: dict[str, Any], key: str, default: bool) -> bool:
    """读取布尔配置。

    :param section: 配置段。
    :param key: 键名。
    :param default: 缺省值。
    :return: 布尔值。
    :raises ConfigurationError: 值的类型不是布尔。
    """
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"配置项 {key} 必须是 true 或 false")
    return value


def _origins(section: dict[str, Any], key: str) -> tuple[str, ...]:
    """读取跨域来源列表。

    :param section: 配置段。
    :param key: 键名。
    :return: 来源元组。
    :raises ConfigurationError: 值不是字符串数组。
    """
    value = section.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ConfigurationError(f"配置项 {key} 必须是字符串数组")
    return tuple(item for item in value if item)


def settings_from_dict(raw: dict[str, Any]) -> Settings:
    """把 JSON 配置转换为 ``Settings``。

    :param raw: 已解析的 JSON 对象。
    :return: 运行配置。
    :raises ConfigurationError: 配置结构或字段类型不合法。
    """
    database_raw = _section(raw, "database")
    fallback_raw = _section(raw, "fallback")
    api_raw = _section(raw, "api")
    database = DatabaseSettings(
        host=_text(database_raw, "host", "127.0.0.1"),
        port=_integer(database_raw, "port", 3306, minimum=1),
        name=_text(database_raw, "name", "futures_reports"),
        user=_text(database_raw, "user", "root"),
        password=_text(database_raw, "password", ""),
        connect_timeout=_integer(database_raw, "connect_timeout", 10, minimum=1),
    )
    fallback = FallbackSettings(
        endpoint=_text(fallback_raw, "endpoint"),
        api_token=_text(fallback_raw, "api_token"),
        model_id=_text(fallback_raw, "model_id", "course-fallback"),
        prompt_version=_text(fallback_raw, "prompt_version", "fallback-v1"),
        connect_timeout=_number(fallback_raw, "connect_timeout", 10.0),
        read_timeout=_number(fallback_raw, "read_timeout", 60.0),
        max_response_bytes=_integer(
            fallback_raw, "max_response_bytes", 262144, minimum=1024
        ),
        max_fragments=_integer(fallback_raw, "max_fragments", 3, minimum=1),
        enabled=_boolean(fallback_raw, "enabled", True),
    )
    return Settings(
        database=database,
        fallback=fallback,
        data_root=Path(_text(raw, "data_root", "data")),
        analyzer_version=_text(raw, "analyzer_version", "backend-1.0.0"),
        parser_timeout_seconds=_integer(
            raw, "parser_timeout_seconds", 60, minimum=1
        ),
        max_attachment_depth=_integer(
            raw, "max_attachment_depth", 2, minimum=0
        ),
        max_attachments=_integer(raw, "max_attachments", 8, minimum=0),
        analyze_workers=_integer(raw, "analyze_workers", 1, minimum=1),
        api_page_size=_integer(api_raw, "page_size", 50, minimum=1),
        api_max_page_size=_integer(api_raw, "max_page_size", 100, minimum=1),
        api_cors_origins=_origins(api_raw, "cors_origins"),
    )


def load_settings(path: str | Path | None = None) -> Settings:
    """读取配置文件并生成运行配置。

    默认路径不存在时返回默认配置，方便只跑单元测试；显式传入的路径不存在时
    直接报错，避免命令行拼错文件名却静默使用默认值。

    :param path: 配置文件路径，``None`` 表示 ``config.local.json``。
    :return: 运行配置。
    :raises ConfigurationError: 文件不存在、JSON 非法或字段类型不合法。
    """
    explicit = path is not None
    target = Path(path) if explicit else default_config_path()
    if not target.is_file():
        if explicit:
            raise ConfigurationError(f"配置文件不存在：{target}")
        return Settings()
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise ConfigurationError(f"无法读取配置文件：{target}") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"配置文件不是合法 JSON：{target}（第 {error.lineno} 行）"
        ) from error
    if not isinstance(raw, dict):
        raise ConfigurationError("配置文件顶层必须是 JSON 对象")
    return settings_from_dict(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的配置。

    :return: 默认配置文件对应的运行配置。
    """
    return load_settings()


def reset_settings_cache() -> None:
    """清除配置缓存，修改配置文件或测试后调用。"""
    get_settings.cache_clear()
