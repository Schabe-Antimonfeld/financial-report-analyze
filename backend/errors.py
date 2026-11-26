"""统一异常与错误码。

外部接口（CLI、FastAPI、交接 JSON）只暴露错误码字符串，避免把驱动异常、
堆栈或原始正文带到响应里。
"""
from __future__ import annotations


class BackendError(Exception):
    """后端可预期异常的基类。

    :param message: 给日志看的说明。
    :param code: 交接用的错误码，默认取类名对应常量。
    """

    code = "backend_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        """创建后端异常。

        :param message: 给日志看的说明。
        :param code: 覆盖类级默认错误码。
        """
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code


class ConfigurationError(BackendError):
    """环境变量或运行配置缺失、取值非法。"""

    code = "configuration_error"


class DatabaseUnavailableError(BackendError):
    """数据库驱动缺失或连接失败。"""

    code = "database_unavailable"


class DatabaseOperationError(BackendError):
    """SQL 执行失败，且调用方无法继续处理。"""

    code = "database_error"


class ArticleNotFoundError(BackendError):
    """按文章 ID 或指纹找不到原文。"""

    code = "article_not_found"


class AttachmentPathError(BackendError):
    """附件引用不是资料根下的合法相对路径。

    :param message: 具体原因。
    :param code: ``attachment_remote``、``attachment_empty`` 等。
    """

    def __init__(self, message: str, *, code: str) -> None:
        """创建附件路径异常。

        :param message: 具体原因。
        :param code: ``attachment_remote``、``attachment_empty`` 等。
        """
        super().__init__(message, code=code)


class ParserUnavailableError(BackendError):
    """解析模块缺失或未注册对应格式。"""

    code = "parser_unavailable"


class ExtractionError(BackendError):
    """解析器内部失败，无法交付任何块。"""

    code = "parse_error"


class FallbackError(BackendError):
    """模型兜底的配置或响应错误。"""

    code = "fallback_error"


class CursorError(BackendError):
    """列表游标非法或与当前筛选不一致。"""

    code = "cursor_invalid"


# 流水线状态与原因码，供 CLI、日志和 save_opinions 共用。
STATUS_INSERTED = "inserted"
STATUS_UPDATED = "updated"
STATUS_SKIPPED = "skipped"
STATUS_SAVED = "saved"
STATUS_STALE = "stale"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"

REASON_NO_EXPLICIT_VIEW = "no_explicit_view"
REASON_TARGET_REVIEW = "target_review"
REASON_LAYOUT_REVIEW = "layout_review"
REASON_DIRECTION_CONFLICT = "direction_conflict"
REASON_MODEL_ABSTAINED = "model_abstained"
REASON_MODEL_INVALID = "model_invalid"
REASON_MODEL_ERROR = "model_error"
