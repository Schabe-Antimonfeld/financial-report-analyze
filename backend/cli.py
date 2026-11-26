"""命令行入口。

用法示例::

    python -m backend.cli ingest --limit 2 --dry-run
    python -m backend.cli ingest --limit 2
    python -m backend.cli analyze --article-id 101 --dry-run
    python -m backend.cli analyze --article-id 101
    python -m backend.cli analyze --limit 20 --workers 4
    python -m backend.cli api --port 8000

输出为 UTF-8 JSON 行，便于保存成回归记录或交给 5 号联调。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from backend.config import Settings, load_settings
from backend.errors import BackendError
from backend.pipeline import (
    analyze_article,
    analyze_articles,
    discover_article_ids,
    extract_data_root,
    preview_scope,
    preview_tasks,
)
from backend.repository import ingest_file, iter_discovered_files, probe_file

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """创建命令行解析器。

    :return: 含 ``ingest``、``analyze``、``api`` 三个子命令的解析器。
    """
    parser = argparse.ArgumentParser(
        prog="backend.cli",
        description="期货研报观点提取：原文导入、批量分析与查询服务",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出调试日志",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="配置文件路径，默认仓库根目录 config.local.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="导入本地原文")
    ingest.add_argument(
        "--path",
        action="append",
        default=[],
        help="指定文件，可重复；省略时按资料根目录发现",
    )
    ingest.add_argument("--limit", type=int, default=None, help="最多导入几个文件")
    ingest.add_argument("--data-root", type=Path, default=None, help="资料根目录")
    ingest.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出发现的路径与格式，不写库",
    )

    analyze = subparsers.add_parser("analyze", help="按文章 ID 或批量分析")
    target = analyze.add_mutually_exclusive_group(required=True)
    target.add_argument("--article-id", type=int, help="单篇文章 ID")
    target.add_argument("--limit", type=int, help="按 ID 升序分析前若干篇")
    analyze.add_argument(
        "--after-id",
        type=int,
        default=0,
        help="配合 --limit 跳过已处理的 ID",
    )
    analyze.add_argument(
        "--dry-run",
        action="store_true",
        help="读原文、解析并跑规则，不调用模型、不写事务",
    )
    analyze.add_argument("--workers", type=int, default=None, help="并发线程数，最多 4")
    analyze.add_argument(
        "--details",
        action="store_true",
        help="输出完整分析结果（含证据与输入版本）",
    )
    analyze.add_argument(
        "--output",
        type=Path,
        default=None,
        help="把结果写入指定 JSON 文件",
    )

    api = subparsers.add_parser("api", help="启动 FastAPI 服务")
    api.add_argument("--host", default="127.0.0.1", help="监听地址")
    api.add_argument("--port", type=int, default=8000, help="监听端口")
    api.add_argument("--reload", action="store_true", help="开发模式热重载")
    return parser


def _print_json(payload: Any, *, pretty: bool = False) -> None:
    """以 UTF-8 JSON 输出一条记录。

    :param payload: 可序列化的结构。
    :param pretty: 是否缩进输出。
    """
    if pretty:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    else:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    print(text)


def _configure_logging(verbose: bool) -> None:
    """配置日志输出到标准错误。

    :param verbose: 是否输出调试日志。
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        force=True,
    )


def run_ingest(args: argparse.Namespace, settings: Settings) -> int:
    """执行导入子命令。

    :param args: 解析后的参数。
    :param settings: 运行配置。
    :return: 进程退出码。
    """
    data_root = extract_data_root(args.data_root)
    try:
        if args.path:
            targets = [Path(item) for item in args.path]
        else:
            targets = [path for path, _relative in iter_discovered_files(data_root)]
    except OSError as error:
        print(f"无法读取资料根目录：{error}", file=sys.stderr)
        return 1
    if args.limit is not None:
        targets = targets[: args.limit]

    if args.dry_run:
        for path in targets:
            try:
                _print_json(probe_file(path, data_root=data_root))
            except OSError as error:
                _print_json(
                    {
                        "relative_path": path.name,
                        "status": "failed",
                        "error_code": "file_unreadable",
                        "detail": str(error),
                    }
                )
        _print_json(
            {
                "status": "dry_run",
                "file_count": len(targets),
                "data_root": str(data_root),
            }
        )
        return 0

    failures = 0
    for path in targets:
        try:
            result = ingest_file(path, data_root=data_root, settings=settings)
        except Exception as error:  # noqa: BLE001 - 单篇失败继续下一篇
            if isinstance(error, BackendError):
                LOGGER.error("导入失败：%s %s", path, error.message)
            else:
                LOGGER.exception("导入失败：%s", path)
            failures += 1
            _print_json(
                {
                    "relative_path": str(path),
                    "status": "failed",
                    "error_code": getattr(error, "code", "ingest_failed"),
                }
            )
            continue
        _print_json(result.to_dict())
    return 1 if failures else 0


def _preview_payload(result: Any) -> dict[str, Any]:
    """把一次预览分析压缩成交接 JSON。

    :param result: ``IntegrationResult`` 风格的 ``AnalysisResult``。
    :return: 预览字典。
    """
    return {
        "article_id": result.article_id,
        "format": result.input_parts[0].format if result.input_parts else None,
        "input_hash": result.input_hash,
        "attachment_count": sum(
            1 for part in result.input_parts if part.role == "attachment"
        ),
        "metadata": result.metadata.to_dict() if result.metadata else None,
        "block_kinds": result.stats.get("block_kinds", {}),
        "scope_count": len(result.scopes),
        "scopes": [preview_scope(scope) for scope in result.scopes],
        "fallback_tasks": preview_tasks(result.fallback_tasks),
        "issues": [issue.to_dict() for issue in result.issues],
        "stats": result.stats,
    }


def run_analyze(args: argparse.Namespace, settings: Settings) -> int:
    """执行分析子命令。

    :param args: 解析后的参数。
    :param settings: 运行配置。
    :return: 进程退出码。
    """
    if args.article_id is not None:
        return _analyze_one(args.article_id, args, settings)

    article_ids = discover_article_ids(limit=args.limit, settings=settings)
    article_ids = [item for item in article_ids if item > args.after_id]
    if not article_ids:
        print("没有可分析的文章", file=sys.stderr)
        return 0

    if args.dry_run:
        failures = 0
        previews: list[dict[str, Any]] = []
        for article_id in article_ids:
            try:
                result = analyze_article(article_id, dry_run=True, settings=settings)
            except Exception as error:  # noqa: BLE001 - 批量继续下一篇
                if isinstance(error, BackendError):
                    LOGGER.error(
                        "预览失败：article_id=%s %s", article_id, error.message
                    )
                else:
                    LOGGER.exception("预览失败：article_id=%s", article_id)
                failures += 1
                _print_json(
                    {
                        "article_id": article_id,
                        "status": "failed",
                        "error_code": getattr(error, "code", "dry_run_failed"),
                        "detail": str(error),
                    }
                )
                continue
            payload = _preview_payload(result)
            previews.append(payload)
            _print_json(payload)
        if args.output is not None:
            _write_lines(previews, args.output)
        return 1 if failures else 0

    summaries: list[dict[str, Any]] = analyze_articles(
        article_ids,
        dry_run=False,
        settings=settings,
        workers=args.workers,
    )
    for summary in summaries:
        _print_json(summary)
    if args.output is not None:
        _write_lines(summaries, args.output)
    failed = any(item.get("status") in {"failed", "stale"} for item in summaries)
    return 1 if failed else 0


def _analyze_one(
    article_id: int,
    args: argparse.Namespace,
    settings: Settings,
) -> int:
    """分析单篇文章并按需输出。

    :param article_id: 文章 ID。
    :param args: 解析后的参数。
    :param settings: 运行配置。
    :return: 进程退出码。
    """
    try:
        result = analyze_article(
            article_id,
            dry_run=args.dry_run,
            settings=settings,
        )
    except Exception as error:  # noqa: BLE001 - CLI 需要打印失败原因
        if isinstance(error, BackendError):
            LOGGER.error("分析失败：article_id=%s %s", article_id, error.message)
        else:
            LOGGER.exception("分析失败：article_id=%s", article_id)
        _print_json(
            {
                "article_id": article_id,
                "status": "failed",
                "error_code": getattr(error, "code", "analyze_failed"),
                "detail": str(error),
            }
        )
        return 1
    payload = _preview_payload(result) if args.dry_run else result.to_dict()
    _write_payload(payload, output=args.output)
    if result.save_result is not None and result.save_result.status != "saved":
        return 1
    return 0


def _write_payload(payload: dict[str, Any], *, output: Path | None) -> None:
    """输出单条完整结果。

    :param payload: 结果字典。
    :param output: 写入文件，``None`` 时打印。
    """
    if output is None:
        _print_json(payload)
        return
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"结果已写入 {output}")


def _write_lines(items: list[dict[str, Any]], output: Path) -> None:
    """把批量汇总写入 JSON 数组文件。

    :param items: 汇总记录。
    :param output: 输出文件。
    """
    output.write_text(
        json.dumps(items, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"批量汇总已写入 {output}")


def run_api(args: argparse.Namespace, settings: Settings) -> int:
    """启动 FastAPI 服务。

    ``--reload`` 时由 uvicorn 重新导入 ``backend.api:app``，该入口读取默认的
    ``config.local.json``；不使用 reload 时直接传入当前配置。

    :param args: 解析后的参数。
    :param settings: 运行配置。
    :return: 进程退出码。
    """
    try:
        import uvicorn
    except ImportError:
        print("未安装 uvicorn，请执行 pip install -r requirements.txt", file=sys.stderr)
        return 1
    if args.reload:
        uvicorn.run(
            "backend.api:app",
            host=args.host,
            port=args.port,
            reload=True,
        )
        return 0
    from backend.api import create_app

    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行主入口。

    :param argv: 参数列表，``None`` 时取 ``sys.argv``。
    :return: 进程退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        settings = load_settings(args.config)
    except BackendError as error:
        print(f"读取配置失败：{error.message}", file=sys.stderr)
        return 1
    if args.command == "ingest":
        return run_ingest(args, settings)
    if args.command == "analyze":
        return run_analyze(args, settings)
    if args.command == "api":
        return run_api(args, settings)
    parser.error("未知子命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
