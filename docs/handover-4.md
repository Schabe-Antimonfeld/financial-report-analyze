# 阶段 06 交付清单（4 号 → 组内）

本文件说明「哪份文件交给谁、用来做什么」。源码都在 `backend/`，运行方式与接口
说明见 [06-python-batch-api.md](06-python-batch-api.md)。

## 一、交给 5 号（司浩然，Vue 展示）

| 文件 | 用途 |
| --- | --- |
| [samples/opinions_list.json](samples/opinions_list.json) | `GET /opinions` 列表响应样例（含 `items`、`next_cursor`） |
| [samples/opinion_detail.json](samples/opinion_detail.json) | `GET /opinions/{id}` 详情与证据样例 |
| [samples/opinion_trends.json](samples/opinion_trends.json) | `GET /opinion-trends` 日度分布样例 |
| [samples/evidence_html.json](samples/evidence_html.json) | HTML 证据的文本摘录响应样例 |
| [06-python-batch-api.md](06-python-batch-api.md) 的「接口」章节 | 参数、分页游标、错误码、统计口径、Vite 代理说明 |
| 服务地址 `http://127.0.0.1:8000/docs` | 联调时在线查看真实字段 |

另外口头/文档告知三点：

1. 前端请求 `/api/opinions`，Vite 代理去掉 `/api` 前缀，FastAPI 实际路径是 `/opinions`。
2. 游标由服务器生成，换筛选必须从第一页重查；旧游标会返回 422 `cursor_invalid`。
3. `ratios` 三个值可能同时为 `null`，页面显示「无明确观点」，不要画 0% 假条。

如果 5 号要自己起服务：再给 [requirements.txt](requirements.txt)、
[backend/api.py](backend/api.py)、[backend/api_queries.py](backend/api_queries.py)，
并复制 `config.example.json` 为 `config.local.json` 填写数据库参数；否则只给
样例和 `/docs` 即可。

## 二、交给 1 号（厉宸昊，项目经理 / 验收）

| 文件 | 用途 |
| --- | --- |
| [06-python-batch-api.md](06-python-batch-api.md) | 环境准备、导入/分析/启动命令、验收对照表 |
| [backend/cli.py](backend/cli.py) | 可运行的 `ingest` / `analyze` / `api` 入口 |
| [backend/pipeline.py](backend/pipeline.py) | 编排流程说明（读库、解析、兜底、保存） |
| [backend/repository.py](backend/repository.py) | 对接 `store.py` 的适配层（连接、异常、只读查询） |
| [requirements.txt](requirements.txt) | 依赖版本 |
| [config.example.json](config.example.json) | 本地配置模板（空密码，可提交）；`config.local.json` 不入库 |
| [backend/database/001_raw.sql](backend/database/001_raw.sql)、[backend/database/002_analysis.sql](backend/database/002_analysis.sql) | **联调参考**建表脚本；正式迁移以 1 号的 001/002 为准，若已有迁移请勿覆盖 |
| 运行生成的回归 JSON | 由 `analyze --limit N --output regression.json` 产出，不随源码提交 |
| [tests/](tests/test_pipeline.py) 与测试结果 | 110 个单元测试通过，作为自测证据 |

回归记录建议用这个命令生成（需要真实 MySQL 与 `data/` 样本）：

```powershell
python -m backend.cli analyze --limit 20 --workers 1 --output regression.json
```

`regression.json` 每篇含：文章 ID、实际格式、范围数、观点数、模型调用数、
可重试范围数、耗时、`attempt_id`、失败原因；证据回查结果需要 1 号用
`GET /evidence/{id}/source` 抽查后补一列。

## 三、交给 2 号（庞博，PDF/PNG）与 3 号（董天皓，HTML/规则/兜底）

只交接口契约，不要求他们使用我的基线实现：

| 文件 | 用途 |
| --- | --- |
| [backend/models.py](backend/models.py) | `RawArticle`、`Block`、`ExtractionResult`、`Issue` 字段定义 |
| [backend/parsers/__init__.py](backend/parsers/__init__.py) | 格式分流与 `extract_pdf/html/png` 的调用约定 |
| [backend/textutil.py](backend/textutil.py) | `normalize_text`（NFKC + 空白归一）统一口径 |

对接方式：把交付的 `extract_pdf(article)`、`extract_png(article)`、
`extract_html(article)` 覆盖 `backend/parsers/` 同名函数，返回
`ExtractionResult(blocks, attachment_refs, issues)` 即可，编排层不用改。

给 3 号额外一份兜底联调基线：

| 文件 | 用途 |
| --- | --- |
| [backend/fallback.py](backend/fallback.py) | `plan_fallback` / `infer_direction` / SSE 拼接 / 引文校验的可运行基线 |
| [tests/test_fallback.py](tests/test_fallback.py) | 断流、改标点、范围不一致等验收样例 |

3 号交付正式实现后替换同名函数；`FallbackTask`、`FallbackResult` 字段不要改，
否则缓存键与 `model_attempts` 会失效。

## 四、不需要交付的文件

- `tests/` 全部测试：给 1 号做自测证据即可，不要求前端或解析同学运行。
- `config.local.json`：本机数据库密码与模型令牌，已列入 `.gitignore`，绝不上传。
- `.env`、`data/`、`samples/` 以外的运行输出、`__pycache__`、`*.pyc`。
- 1 号已经维护的 `backend/store.py`：不要用本地副本覆盖，也不要重复上传。

## 五、交付前还需补的三件事

1. 用真实 MySQL 跑通宝城、大越两篇，把实际文章 ID、格式、指纹填进回归记录。
2. 用最大样本（约 39.4 MiB）实测一次 MySQL 读写，确认
   `max_allowed_packet` 与客户端配置一致。
3. 给 5 号的服务地址以实际启动的 `host:port` 为准，并把 `/docs` 截图或地址
   一并附上。
