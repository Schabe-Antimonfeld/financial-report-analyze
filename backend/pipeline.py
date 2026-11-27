"""分析编排。

把阶段 01～05 交付的函数串成一条命令：读原文、按格式解析、读取附件、提取
元数据与规则观点、规划并执行模型兜底、在事务里保存。预览模式只读到规则结果
和拟兜底范围，不调用模型、不写分析记录。
"""
from __future__ import annotations

import logging
import posixpath
import time
from collections import deque
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from backend.analysis_store import (
    begin_attempt,
    compute_input_hash,
    find_cached_model_attempt,
    finish_attempt,
    record_model_attempt,
    save_opinions,
)
from backend.config import Settings, get_settings
from backend.errors import (
    ArticleNotFoundError,
    AttachmentPathError,
    BackendError,
    REASON_MODEL_ABSTAINED,
    REASON_MODEL_ERROR,
    REASON_MODEL_INVALID,
    STATUS_FAILED,
    STATUS_SAVED,
    STATUS_STALE,
)
from backend.fallback import (
    build_fallback_result,
    choose_fragments,
    infer_direction,
    plan_fallback,
)
from backend.models import (
    AnalysisResult,
    Block,
    ExtractionResult,
    FallbackResult,
    FallbackTask,
    InputPart,
    Issue,
    RawArticle,
    RuleResult,
    ScopeResult,
)
from backend.parsers import extract_blocks
from backend.repository import find_article_id, load_article
from backend.rules import extract_metadata, extract_opinions
from backend.textutil import sha256_hex, canonical_json

LOGGER = logging.getLogger(__name__)

MAX_ANALYZE_WORKERS = 4
FATAL_CODES = frozenset({"parser_unavailable", "parse_error"})


def attachment_path(parent_path: str, href: str) -> str:
    """把 HTML 中的附件引用解析成资料根下的相对路径。

    :param parent_path: 当前页面的规范相对路径。
    :param href: 链接原始 ``href``。
    :return: 规范相对路径。
    :raises AttachmentPathError: 远端地址、空路径、非法字符或跳出资料根。
    """
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        raise AttachmentPathError("附件为远端地址", code="attachment_remote")
    try:
        path = unquote(parts.path, errors="strict")
    except UnicodeError as error:
        raise AttachmentPathError("附件路径编码非法", code="attachment_invalid_path") from error
    if not path:
        raise AttachmentPathError("附件路径为空", code="attachment_empty")
    if path.startswith("/") or any(char in path for char in ("\\", ":", "\x00")):
        raise AttachmentPathError("附件路径非法", code="attachment_invalid_path")
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(parent_path), path))
    if joined in {".", ".."} or joined.startswith("../"):
        raise AttachmentPathError("附件跳出资料根", code="attachment_outside_data")
    return joined


class _ReportSources:
    """一篇主报告及其附件的解析结果汇总。"""

    def __init__(self) -> None:
        """初始化主文与附件的汇总容器。"""
        self.blocks: list[Block] = []
        self.parts: list[InputPart] = []
        self.issues: list[Issue] = []
        self.chains: dict[tuple[int, str], list[dict[str, Any]]] = {}
        self.fatal = False

    def add_source(
        self,
        article: RawArticle,
        extraction: ExtractionResult,
        chain: list[dict[str, Any]],
    ) -> None:
        """登记一个来源的块、版本与问题。

        附件块号加上来源前缀，避免与主文或另一个附件的 ``b-0001`` 冲突；
        块号在同一个原文版本内仍然稳定。

        :param article: 来源原文。
        :param extraction: 解析结果。
        :param chain: 该来源的附件引用链。
        """
        if chain:
            prefix = f"a{article.article_id}-"
            for block in extraction.blocks:
                if block.block_id and not block.block_id.startswith(prefix):
                    block.block_id = f"{prefix}{block.block_id}"
        self.blocks.extend(extraction.blocks)
        self.issues.extend(extraction.issues)
        self.chains[(article.article_id, article.content_hash)] = chain
        self.parts.append(
            InputPart(
                role="main" if not chain else "attachment",
                relative_path=article.relative_path,
                article_id=article.article_id,
                content_hash=article.content_hash,
                format=article.format,
            )
        )


def collect_report(article: RawArticle, settings: Settings) -> _ReportSources:
    """解析主文与所有已入库附件。

    :param article: 主文原文。
    :param settings: 运行配置。
    :return: 汇总后的块、输入版本与问题。
    """
    sources = _ReportSources()
    max_depth, max_attachments = settings.attachment_limits()
    queue: deque[tuple[RawArticle, list[dict[str, Any]], int]] = deque(
        [(article, [], 0)]
    )
    seen: set[tuple[int, str]] = set()
    attachment_paths: set[str] = set()

    while queue:
        current, chain, depth = queue.popleft()
        source_key = (current.article_id, current.content_hash)
        if source_key in seen:
            continue
        seen.add(source_key)
        extraction = extract_blocks(current)
        if depth == 0 and any(
            issue.code in FATAL_CODES for issue in extraction.issues
        ):
            sources.fatal = True
        sources.add_source(current, extraction, chain)

        for ref in extraction.attachment_refs:
            try:
                relative_path = attachment_path(current.relative_path, ref.href)
            except AttachmentPathError as error:
                sources.issues.append(Issue(error.code, None, ref.href))
                continue
            if relative_path in attachment_paths:
                continue
            if depth + 1 > max_depth:
                sources.issues.append(
                    Issue("attachment_depth_exceeded", None, relative_path)
                )
                continue
            if len(attachment_paths) >= max_attachments:
                sources.issues.append(
                    Issue("attachment_limit_exceeded", None, relative_path)
                )
                continue
            attachment_paths.add(relative_path)
            attachment_id = find_article_id(relative_path, settings=settings)
            if attachment_id is None:
                sources.parts.append(
                    InputPart(
                        role="attachment",
                        relative_path=relative_path,
                        article_id=None,
                        content_hash=None,
                    )
                )
                sources.issues.append(Issue("attachment_missing", None, relative_path))
                continue
            try:
                attachment = load_article(attachment_id, settings=settings)
            except ArticleNotFoundError:
                sources.parts.append(
                    InputPart(
                        role="attachment",
                        relative_path=relative_path,
                        article_id=None,
                        content_hash=None,
                    )
                )
                sources.issues.append(Issue("attachment_missing", None, relative_path))
                continue
            next_chain = chain + [
                {
                    "raw_article_id": current.article_id,
                    "content_hash": current.content_hash,
                    "relative_path": current.relative_path,
                    "location": ref.location,
                }
            ]
            queue.append((attachment, next_chain, depth + 1))

    for order, block in enumerate(sources.blocks, start=1):
        block.order = order
    return sources


def _attach_evidence_chains(
    scopes: list[ScopeResult],
    chains: dict[tuple[int, str], list[dict[str, Any]]],
) -> None:
    """把附件引用链补到来自附件的证据上。

    :param scopes: 规则范围。
    :param chains: ``(文章 ID, 指纹) -> 引用链``。
    """
    for scope in scopes:
        for evidence in scope.evidence:
            key = (evidence.raw_article_id, evidence.content_hash)
            chain = chains.get(key)
            if chain:
                evidence.attachment_chain = chain


def _merge_fallback(scope: ScopeResult, result: FallbackResult) -> None:
    """把兜底结论合并进规则范围。

    :param scope: 规则范围，原地修改。
    :param result: 兜底结果。
    """
    if result.status == "inferred":
        scope.direction = result.direction
        scope.source = "llm"
        scope.statement_type = "unconditional"
        scope.review_state = "recognized"
        scope.reason_code = None
        scope.inference_reason = result.inference_reason
        scope.evidence = result.resolved_evidence
        return
    if result.status == "abstained":
        scope.reason_code = REASON_MODEL_ABSTAINED
        scope.review_state = "unresolved"
        return
    if result.status == "invalid":
        scope.reason_code = REASON_MODEL_INVALID
        scope.review_state = "unresolved"
        return
    error_code = result.error_code or "unknown"
    scope.reason_code = f"{REASON_MODEL_ERROR}:{error_code}"[:64]
    scope.review_state = "unresolved"


def _run_fallback(
    result: AnalysisResult,
    rule_result: RuleResult,
    tasks: list[FallbackTask],
    settings: Settings,
    stats: dict[str, Any],
    completion: Any | None = None,
) -> None:
    """逐个执行兜底任务并合并结果。

    :param result: 分析结果，用于写统计。
    :param rule_result: 规则结果，原地合并。
    :param tasks: 兜底任务。
    :param settings: 运行配置。
    :param stats: 统计字典。
    :param completion: 可注入的模型替身，签名 ``(prompt) -> 文本``。
    """
    scopes = {scope.scope_id: scope for scope in rule_result.scopes}
    model_calls = 0
    cached = 0
    retryable: list[str] = []
    started = time.perf_counter()
    for task in tasks:
        scope = scopes.get(task.scope_id)
        if scope is None:
            continue
        cached_row = find_cached_model_attempt(
            result.article_id,
            scope_id=task.scope_id,
            fragment_hash=task.fragment_hash,
            model_id=settings.fallback.model_id,
            prompt_version=settings.fallback.prompt_version,
            settings=settings,
        )
        if cached_row is not None:
            cached += 1
            cached_result = build_fallback_result(
                task, cached_row["validated_result"] or {}
            )
            _merge_fallback(scope, cached_result)
            continue

        fallback_result = infer_direction(
            task, settings=settings, completion=completion
        )
        model_calls += 1
        _merge_fallback(scope, fallback_result)
        if fallback_result.retryable:
            retryable.append(task.scope_id)
        if result.attempt_id is not None:
            record_model_attempt(
                result.attempt_id,
                scope_id=task.scope_id,
                fragment_hash=task.fragment_hash,
                model_id=settings.fallback.model_id,
                prompt_version=settings.fallback.prompt_version,
                status=fallback_result.status,
                error_code=fallback_result.error_code,
                validated_result=(
                    {
                        "scope_id": fallback_result.scope_id,
                        "direction": fallback_result.direction,
                        "evidence": fallback_result.evidence,
                        "inference_reason": fallback_result.inference_reason,
                    }
                    if fallback_result.status in {"inferred", "abstained"}
                    else None
                ),
                settings=settings,
            )
    stats["model_calls"] = model_calls
    stats["model_cache_hits"] = cached
    stats["retryable_scopes"] = retryable
    stats["fallback_ms"] = round((time.perf_counter() - started) * 1000, 2)


def _fallback_input_hash(article: RawArticle) -> str:
    """在无法完成解析时给出一个可追溯的输入指纹。

    :param article: 主文原文。
    :return: 64 位十六进制指纹。
    """
    payload = canonical_json(
        {
            "role": "main",
            "relative_path": article.relative_path,
            "article_id": article.article_id,
            "content_hash": article.content_hash,
        }
    )
    return sha256_hex(payload)


def analyze_article(
    article_id: int,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
    completion: Any | None = None,
) -> AnalysisResult:
    """分析一篇报告。

    :param article_id: 文章 ID。
    :param dry_run: 只读原文、解析与规则，不调用模型、不写库。
    :param settings: 运行配置。
    :param completion: 可注入的模型替身，签名 ``(prompt) -> 文本``。
    :return: 分析结果；``dry_run`` 时不写分析记录与观点。
    :raises ArticleNotFoundError: 文章不存在。
    """
    settings = settings or get_settings()
    stats: dict[str, Any] = {"dry_run": dry_run, "model_calls": 0}
    total_started = time.perf_counter()

    read_started = time.perf_counter()
    article = load_article(article_id, settings=settings)
    stats["read_ms"] = round((time.perf_counter() - read_started) * 1000, 2)

    result = AnalysisResult(
        article_id=article_id,
        analyzer_version=settings.analyzer_version,
        stats=stats,
    )
    attempt_id: int | None = None
    try:
        parse_started = time.perf_counter()
        sources = collect_report(article, settings)
        stats["parse_ms"] = round((time.perf_counter() - parse_started) * 1000, 2)
        stats["block_count"] = len(sources.blocks)
        stats["block_kinds"] = summarize_blocks(sources.blocks)
        stats["attachment_count"] = sum(
            1 for part in sources.parts if part.role == "attachment"
        )
        stats["extraction_issue_count"] = len(sources.issues)

        result.input_parts = sources.parts
        result.input_hash = compute_input_hash(
            [part.to_dict() for part in sources.parts]
        )
        result.extraction_issues = list(sources.issues)
        if sources.fatal or (not sources.blocks and sources.issues):
            fatal_code = next(
                (
                    issue.code
                    for issue in sources.issues
                    if issue.code in FATAL_CODES
                ),
                sources.issues[0].code
                if sources.issues
                else "unreadable_article",
            )
            raise BackendError(fatal_code, code="unreadable_article")

        if not dry_run:
            attempt_id = begin_attempt(
                article_id,
                result.input_hash,
                settings.analyzer_version,
                settings=settings,
            )
            result.attempt_id = attempt_id

        metadata = extract_metadata(article, sources.blocks)
        rule_started = time.perf_counter()
        rule_result = extract_opinions(sources.blocks, metadata)
        stats["rules_ms"] = round((time.perf_counter() - rule_started) * 1000, 2)
        _attach_evidence_chains(rule_result.scopes, sources.chains)

        tasks = plan_fallback(
            rule_result, max_fragments=settings.fallback.max_fragments
        )
        result.metadata = metadata
        result.scopes = rule_result.scopes
        result.issues = list(rule_result.issues) + list(sources.issues)
        result.fallback_tasks = tasks
        stats["scope_count"] = len(rule_result.scopes)
        stats["fallback_task_count"] = len(tasks)

        if dry_run:
            stats["model_calls"] = 0
            stats["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
            return result

        if tasks:
            _run_fallback(result, rule_result, tasks, settings, stats, completion)
        result.scopes = rule_result.scopes

        save_started = time.perf_counter()
        save_result = save_opinions(result, settings=settings)
        stats["save_ms"] = round((time.perf_counter() - save_started) * 1000, 2)
        result.save_result = save_result
        stats["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        if save_result.status != STATUS_SAVED:
            LOGGER.warning(
                "观点写入未成功：article_id=%s status=%s error=%s",
                article_id,
                save_result.status,
                save_result.error_code,
            )
            if attempt_id is not None:
                finish_attempt(
                    attempt_id,
                    save_result.status,
                    error_code=save_result.error_code,
                    result_count=0,
                    settings=settings,
                )
        return result
    except Exception as error:
        if not dry_run:
            _record_failure(article, result, attempt_id, error, settings)
        raise


def _record_failure(
    article: RawArticle,
    result: AnalysisResult,
    attempt_id: int | None,
    error: BaseException,
    settings: Settings,
) -> None:
    """把失败原因记录到分析运行表。

    :param article: 主文原文。
    :param result: 当前已有结果。
    :param attempt_id: 已登记的分析号。
    :param error: 失败异常。
    :param settings: 运行配置。
    """
    code = getattr(error, "code", type(error).__name__.lower())
    try:
        active_attempt = attempt_id
        if active_attempt is None:
            active_attempt = begin_attempt(
                article.article_id,
                result.input_hash or _fallback_input_hash(article),
                settings.analyzer_version,
                settings=settings,
            )
        finish_attempt(
            active_attempt,
            STATUS_FAILED,
            error_code=code,
            result_count=0,
            settings=settings,
        )
    except Exception:  # noqa: BLE001 - 记录失败不能覆盖原始异常
        LOGGER.exception("记录分析失败原因时出错：article_id=%s", article.article_id)


def analyze_articles(
    article_ids: Iterable[int],
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
    workers: int | None = None,
) -> list[dict[str, Any]]:
    """批量分析，单篇失败不阻断其他文章。

    :param article_ids: 文章 ID 集合。
    :param dry_run: 预览模式。
    :param settings: 运行配置。
    :param workers: 并发线程数，上限 4，默认取配置。
    :return: 每篇的汇总记录。
    """
    settings = settings or get_settings()
    limit = workers if workers is not None else settings.analyze_workers
    limit = max(1, min(limit, MAX_ANALYZE_WORKERS))
    ordered_ids = list(article_ids)
    if limit == 1:
        return [
            _analyze_one(article_id, dry_run=dry_run, settings=settings)
            for article_id in ordered_ids
        ]
    reports: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=limit) as pool:
        futures = {
            pool.submit(
                _analyze_one, article_id, dry_run=dry_run, settings=settings
            ): article_id
            for article_id in ordered_ids
        }
        for future in as_completed(futures):
            article_id = futures[future]
            try:
                reports[article_id] = future.result()
            except Exception as error:  # noqa: BLE001 - 汇总失败原因
                reports[article_id] = {
                    "article_id": article_id,
                    "status": STATUS_FAILED,
                    "error_code": getattr(error, "code", "unexpected_error"),
                    "model_calls": 0,
                }
    return [reports[article_id] for article_id in ordered_ids]


def _analyze_one(
    article_id: int,
    *,
    dry_run: bool,
    settings: Settings,
) -> dict[str, Any]:
    """分析一篇并压缩成汇总记录。

    :param article_id: 文章 ID。
    :param dry_run: 预览模式。
    :param settings: 运行配置。
    :return: 汇总记录。
    """
    started = time.perf_counter()
    try:
        result = analyze_article(article_id, dry_run=dry_run, settings=settings)
    except Exception as error:  # noqa: BLE001 - 批量需要继续下一篇
        if isinstance(error, BackendError):
            LOGGER.error("分析失败：article_id=%s %s", article_id, error.message)
        else:
            LOGGER.exception("分析失败：article_id=%s", article_id)
        return {
            "article_id": article_id,
            "status": STATUS_FAILED,
            "error_code": getattr(error, "code", type(error).__name__.lower()),
            "model_calls": 0,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    save_status = (
        result.save_result.status
        if result.save_result is not None
        else ("dry_run" if dry_run else STATUS_FAILED)
    )
    retryable_scopes = result.stats.get("retryable_scopes") or []
    if save_status == STATUS_STALE:
        status = STATUS_STALE
    elif save_status == STATUS_SAVED:
        status = STATUS_PARTIAL if retryable_scopes else STATUS_SAVED
    elif dry_run:
        status = "dry_run"
    else:
        status = STATUS_FAILED
    return {
        "article_id": article_id,
        "status": status,
        "format": result.input_parts[0].format if result.input_parts else None,
        "scope_count": len(result.scopes),
        "opinion_count": (
            result.save_result.opinion_count if result.save_result else 0
        ),
        "model_calls": result.stats.get("model_calls", 0),
        "fallback_task_count": len(result.fallback_tasks),
        "retryable_count": len(retryable_scopes),
        "block_count": result.stats.get("block_count", 0),
        "error_code": (
            result.save_result.error_code if result.save_result else None
        ),
        "attempt_id": result.attempt_id,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def discover_article_ids(
    *,
    limit: int | None = None,
    settings: Settings | None = None,
) -> list[int]:
    """按 ID 升序取待分析文章。

    :param limit: 最多条数。
    :param settings: 运行配置。
    :return: 文章 ID 列表。
    """
    from backend.repository import list_article_ids

    return list_article_ids(limit=limit, settings=settings)


def extract_data_root(path: str | Path | None) -> Path:
    """解析资料根目录参数。

    :param path: 命令行传入的目录，``None`` 时取配置。
    :return: 资料根目录。
    """
    return Path(path) if path is not None else get_settings().data_root


def summarize_blocks(blocks: list[Block]) -> dict[str, Any]:
    """统计块类型分布，供 CLI 预览。

    :param blocks: 文本块。
    :return: 类型到数量的映射。
    """
    summary: dict[str, int] = {}
    for block in blocks:
        summary[block.kind] = summary.get(block.kind, 0) + 1
    return summary


def preview_scope(scope: ScopeResult) -> dict[str, Any]:
    """把范围压缩成 CLI 可读结构。

    :param scope: 规则范围。
    :return: 交接用的精简字典。
    """
    return {
        "scope_id": scope.scope_id,
        "direction": scope.direction,
        "source": scope.source,
        "statement_type": scope.statement_type,
        "review_state": scope.review_state,
        "reason_code": scope.reason_code,
        "period_text": scope.period_text,
        "period_definition": scope.period_definition,
        "outlook_text": scope.outlook_text,
        "evidence_quotes": [item.quote for item in scope.evidence],
        "fallback_blocks": [block.block_id for block in scope.fallback_blocks],
    }


def preview_tasks(tasks: list[FallbackTask]) -> list[dict[str, Any]]:
    """把兜底任务压缩成 CLI 可读结构。

    :param tasks: 兜底任务。
    :return: 每个任务的编号、片段号与原文。
    """
    return [task.to_dict() for task in tasks]


def choose_preview_fragments(scope: ScopeResult) -> list[Block]:
    """在没有规则任务时展示拟送出的片段。

    :param scope: 规则范围。
    :return: 至多三条候选片段。
    """
    return choose_fragments(scope.fallback_blocks)
