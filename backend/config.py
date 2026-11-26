"""运行配置。

所有可变配置都从环境变量读取，数据库密码与模型令牌不写入代码，也不出现在
日志中。密码缺失时只在真正连接数据库的那一刻报错。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class DatabaseSettings:
    """MySQL 连接参数，对应阶段 01 的交接表。"""

    host: str = "127.0.0.1"
    port: int = 3306
    name: str = "futures_reports"
    user: str = "root"
    password: str = ""
    connect_timeout: int = 10

    @property
    def is_password_set(self) -> bool:
        """是否已通过环境变量提供密码。"""
        return bool(self.password)


@dataclass(frozen=True)
class FallbackSettings:
    """模型兜底接口配置，对应阶段 05。"""

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


def _env_text(name: str, default: str = "") -> str:
    """读取字符串型环境变量。"""
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    """读取整数型环境变量，非法值回退默认值。"""
    raw_value = _env_text(name)
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """读取浮点型环境变量，非法值回退默认值。"""
    raw_value = _env_text(name)
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量，接受 1/true/yes/on。"""
    raw_value = _env_text(name).casefold()
    if not raw_value:
        return default
    return raw_value in {"1", "true", "yes", "on"}


def _env_origins() -> tuple[str, ...]:
    """读取允许的跨域来源，逗号分隔。"""
    raw_value = _env_text("API_CORS_ORIGINS")
    if not raw_value:
        return ()
    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


def load_settings() -> Settings:
    """按环境变量组装配置。

    :return: 一次运行使用的不可变配置对象。
    """
    database = DatabaseSettings(
        host=_env_text("DB_HOST", "127.0.0.1"),
        port=_env_int("DB_PORT", 3306),
        name=_env_text("DB_NAME", "futures_reports"),
        user=_env_text("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        connect_timeout=_env_int("DB_CONNECT_TIMEOUT", 10),
    )
    fallback = FallbackSettings(
        endpoint=_env_text("LLM_ENDPOINT"),
        api_token=os.environ.get("LLM_API_TOKEN", ""),
        model_id=_env_text("LLM_MODEL_ID", "course-fallback"),
        prompt_version=_env_text("LLM_PROMPT_VERSION", "fallback-v1"),
        connect_timeout=_env_float("LLM_CONNECT_TIMEOUT", 10.0),
        read_timeout=_env_float("LLM_READ_TIMEOUT", 60.0),
        max_response_bytes=_env_int("LLM_MAX_RESPONSE_BYTES", 262144),
        max_fragments=_env_int("LLM_MAX_FRAGMENTS", 3),
        enabled=_env_bool("LLM_ENABLED", True),
    )
    return Settings(
        database=database,
        fallback=fallback,
        data_root=Path(_env_text("DATA_ROOT", "data")),
        analyzer_version=_env_text("ANALYZER_VERSION", "backend-1.0.0"),
        parser_timeout_seconds=_env_int("PARSER_TIMEOUT_SECONDS", 60),
        max_attachment_depth=_env_int("MAX_ATTACHMENT_DEPTH", 2),
        max_attachments=_env_int("MAX_ATTACHMENTS", 8),
        analyze_workers=_env_int("ANALYZE_WORKERS", 1),
        api_page_size=_env_int("API_PAGE_SIZE", 50),
        api_max_page_size=_env_int("API_MAX_PAGE_SIZE", 100),
        api_cors_origins=_env_origins(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的配置。

    :return: 首次调用时按环境变量创建的配置。
    """
    return load_settings()


def reset_settings_cache() -> None:
    """清除配置缓存，测试或改环境变量后调用。"""
    get_settings.cache_clear()
