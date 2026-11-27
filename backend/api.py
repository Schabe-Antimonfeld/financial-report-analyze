"""FastAPI 查询服务。

四个接口对应实施手册第 5 步：观点列表、观点详情、日度趋势、证据原文。
错误统一返回 ``{"error": 错误码, "detail": 说明}``，其中数据库不可用时为 503，
游标或日期区间非法为 422，资源不存在为 404。
"""
from __future__ import annotations

import datetime
import logging
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import __version__
from backend.api_queries import (
    OpinionFilters,
    count_reports,
    get_evidence_source,
    get_opinion,
    get_trend_points,
    list_opinions,
)
from backend.config import Settings, get_settings
from backend.errors import (
    ArticleNotFoundError,
    BackendError,
    ConfigurationError,
    CursorError,
    DatabaseUnavailableError,
)

LOGGER = logging.getLogger(__name__)

DirectionName = Literal["bullish", "bearish", "range"]
SourceName = Literal["rule", "llm"]
ReviewStateName = Literal["recognized", "unresolved", "review"]


def _error_response(status_code: int, code: str, detail: str) -> JSONResponse:
    """构造统一错误响应。

    :param status_code: HTTP 状态码。
    :param code: 错误码。
    :param detail: 可读说明。
    :return: JSON 响应。
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": code, "detail": detail},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用。

    :param settings: 运行配置，测试时可注入。
    :return: 已注册路由、跨域与异常处理的应用。
    """
    active_settings = settings or get_settings()
    app = FastAPI(
        title="期货研报观点",
        version=__version__,
        description="按日期、机构、品种、周期查询研报观点、证据与日度趋势。",
    )
    if active_settings.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(active_settings.api_cors_origins),
            allow_methods=["GET"],
            allow_headers=["*"],
        )

    @app.exception_handler(BackendError)
    def handle_backend_error(_request: object, error: BackendError) -> JSONResponse:
        """把后端异常映射为 HTTP 状态码。"""
        if isinstance(error, (DatabaseUnavailableError, ConfigurationError)):
            return _error_response(503, error.code, error.message)
        if isinstance(error, CursorError):
            return _error_response(422, error.code, error.message)
        if isinstance(error, ArticleNotFoundError):
            return _error_response(404, error.code, error.message)
        LOGGER.error("接口处理失败：%s", error.message)
        return _error_response(500, error.code, error.message)

    @app.exception_handler(StarletteHTTPException)
    def handle_http_error(
        _request: object, error: StarletteHTTPException
    ) -> JSONResponse:
        """把显式抛出的 HTTP 异常也转成统一错误结构。"""
        detail = error.detail
        if isinstance(detail, dict):
            code = str(detail.get("error", "http_error"))
            message = str(detail.get("detail", error.detail))
        else:
            code = f"http_{error.status_code}"
            message = str(detail)
        return _error_response(error.status_code, code, message)

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(
        _request: object, error: RequestValidationError
    ) -> JSONResponse:
        """参数校验失败统一返回 422。"""
        problems = []
        for item in error.errors():
            location = ".".join(str(part) for part in item.get("loc", ()))
            problems.append(f"{location}: {item.get('msg', '参数非法')}")
        return _error_response(422, "validation_error", "；".join(problems))

    @app.get("/health")
    def health() -> dict[str, object]:
        """返回服务与数据库可用状态。"""
        payload: dict[str, object] = {"status": "ok", "version": __version__}
        try:
            payload["reports"] = count_reports(settings=active_settings)
            payload["database"] = "ok"
        except BackendError as error:
            payload["database"] = "unavailable"
            payload["error"] = error.code
        return payload

    @app.get("/opinions")
    def list_opinions_endpoint(
        from_date: Annotated[datetime.date | None, Query()] = None,
        to_date: Annotated[datetime.date | None, Query()] = None,
        institution: Annotated[str | None, Query(max_length=128)] = None,
        product_id: Annotated[str | None, Query(max_length=64)] = None,
        contract: Annotated[str | None, Query(max_length=24)] = None,
        period_key: Annotated[str | None, Query(max_length=64)] = None,
        period_definition: Annotated[str | None, Query(max_length=128)] = None,
        direction: Annotated[DirectionName | None, Query()] = None,
        source: Annotated[SourceName | None, Query()] = None,
        review_state: Annotated[ReviewStateName | None, Query()] = None,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        limit: Annotated[int | None, Query(ge=1)] = None,
    ) -> dict[str, object]:
        """查观点列表，按日期与 ID 倒序分页。"""
        if from_date and to_date and from_date > to_date:
            raise HTTPException(
                status_code=422,
                detail={"error": "date_range_invalid", "detail": "开始日期晚于结束日期"},
            )
        if limit is not None and limit > active_settings.api_max_page_size:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "limit_invalid",
                    "detail": f"limit 不能大于 {active_settings.api_max_page_size}",
                },
            )
        filters = OpinionFilters(
            from_date=from_date,
            to_date=to_date,
            institution=institution,
            product_id=product_id,
            contract=contract,
            period_key=period_key,
            period_definition=period_definition,
            direction=direction,
            source=source,
            review_state=review_state,
        )
        return list_opinions(
            filters,
            cursor=cursor,
            limit=limit,
            settings=active_settings,
        )

    @app.get("/opinions/{opinion_id}")
    def get_opinion_endpoint(opinion_id: int) -> dict[str, object]:
        """查一条观点详情及其证据。"""
        detail = get_opinion(opinion_id, settings=active_settings)
        if detail is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "opinion_not_found", "detail": "观点不存在"},
            )
        return detail

    @app.get("/opinion-trends")
    def get_trends_endpoint(
        product_id: Annotated[str, Query(min_length=1, max_length=64)],
        period_key: Annotated[str, Query(min_length=1, max_length=64)],
        from_date: Annotated[datetime.date, Query()],
        to_date: Annotated[datetime.date, Query()],
        contract: Annotated[str | None, Query(max_length=24)] = None,
        source: Annotated[SourceName, Query()] = "rule",
        period_definition: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, object]:
        """查某品种、周期、来源的日度方向分布。"""
        if from_date > to_date:
            raise HTTPException(
                status_code=422,
                detail={"error": "date_range_invalid", "detail": "开始日期晚于结束日期"},
            )
        return get_trend_points(
            product_id=product_id,
            period_key=period_key,
            from_date=from_date,
            to_date=to_date,
            contract=contract,
            source=source,
            period_definition=period_definition,
            settings=active_settings,
        )

    @app.get("/evidence/{evidence_id}/source")
    def get_evidence_source_endpoint(evidence_id: int) -> Response:
        """按证据返回原文：PDF、PNG 为文件字节，HTML 为文本摘录。"""
        source = get_evidence_source(evidence_id, settings=active_settings)
        if source is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "evidence_not_found", "detail": "证据不存在"},
            )
        if source["format"] == "html":
            return JSONResponse(
                content={
                    "format": "html",
                    "text": source["quote"],
                    "heading_path": source["heading_path"],
                }
            )
        suffix = "pdf" if source["format"] == "pdf" else "png"
        return Response(
            content=source["content"],
            media_type=source["media_type"],
            headers={
                "Content-Disposition": f'inline; filename="evidence-{evidence_id}.{suffix}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=60",
            },
        )

    return app


app = create_app()
