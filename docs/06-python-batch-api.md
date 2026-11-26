# 阶段 06 · Python 批处理与 FastAPI

按实施手册《06-4号-批处理与FastAPI》实现：命令收到文章 ID 后，从 MySQL 读取
原文字节，按实际格式分流解析，读取附件，调用观点规则，按范围决定模型兜底，
最后在事务里保存；FastAPI 把已入库的结果提供给 Vue。

## 目录

```text
backend/
  __init__.py
  config.py           读取 config.local.json（数据库、模型、API）
  errors.py           统一异常与错误码
  textutil.py         NFKC／空白归一化、稳定 JSON、SHA-256
  models.py           RawArticle、Block、RuleResult、FallbackTask 等数据模型
  db.py               连接、显式事务、死锁重试
  store.py            阶段 01（1 号交付）：read_file / store_source / 只读查询
  repository.py       阶段 06 适配层：复用 store.py，统一配置与异常
  parsers/            格式分流；pdf.py、html.py、png.py 为可替换实现
  rules.py            阶段 04：extract_metadata / extract_opinions（基线）
  fallback.py         阶段 05：plan_fallback / infer_direction / SSE / 引文校验
  analysis_store.py   阶段 04：分析记录、模型尝试缓存、save_opinions 事务
  pipeline.py         阶段 06：collect_report / analyze_article / analyze_articles
  cli.py              阶段 06：ingest / analyze / api 命令
  api_queries.py      查询层：筛选、游标、趋势聚合、证据原文
  api.py              阶段 06：FastAPI 四个接口
  database/           001_raw.sql（参考）、002_analysis.sql
samples/              交给 5 号的接口响应样例（列表、详情、趋势、HTML 证据）
tests/                unittest，不需要数据库
config.example.json   本地配置模板（空密码，可提交）
requirements.txt
```

## 环境准备

1. Python 3.12、MySQL 8（`mysql --version`）。数据库字符集 `utf8mb4`。
2. 安装依赖：`python -m pip install -r requirements.txt`。
3. 外部命令（解析器需要，缺失时以问题码返回，不会崩溃）：
   - Poppler：`pdftotext -v`
   - Tesseract：`tesseract --list-langs`，需含 `chi_sim`、`eng`
4. 建库并建表：

   ```powershell
   mysql -u root -p < backend/database/001_raw.sql
   mysql -u root -p futures_reports < backend/database/002_analysis.sql
   ```

5. 生成本地配置：

   ```powershell
   Copy-Item config.example.json config.local.json
   notepad config.local.json      # 填写 database.password、fallback.endpoint 等
   ```

   - 代码**不读取环境变量**，配置只来自 `config.local.json`；也可以用
     `python -m backend.cli --config 其他路径 ...` 指定。
   - `config.local.json` 已列入 `.gitignore`，不会上传；仓库里只保留空密码的
     `config.example.json`。
   - 只跑单元测试时不需要这个文件，缺省配置即可通过。

   `.gitignore` 至少包含：

   ```gitignore
   config.local.json
   .env
   data/
   __pycache__/
   *.py[cod]
   regression.json
   analyze.json
   ```

## 命令

```powershell
# 只发现文件、识别格式，不写库
python -m backend.cli ingest --limit 2 --dry-run

# 导入（按规范相对路径排序），单篇失败继续下一篇
python -m backend.cli ingest --limit 2
python -m backend.cli ingest --path data/data1/20240301/宝城期货_210703.PDF

# 预览：读库、解析、跑规则、列出拟兜底范围；不调用模型、不写事务
python -m backend.cli analyze --article-id 101 --dry-run

# 正式分析并保存
python -m backend.cli analyze --article-id 101
python -m backend.cli analyze --limit 20 --workers 4 --output analyze.json

# 启动查询服务
python -m backend.cli api --port 8000
# 等价：python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000 --reload
```

输出为 UTF-8 JSON 行。`--article-id` 与 `--limit` 互斥；`--details` 输出完整
`AnalysisResult`，包含 `input_parts`、证据、兜底任务与耗时。批处理启动阶段默认
串行，测通后可 `--workers`，上限 4，每个线程各自持连接。

## 接口

`GET /health` 返回 `{"status":"ok","database":"ok","reports":N}`；数据库不可用时
`status` 仍为 `ok`，`database` 为 `unavailable`。

| 方法与路径 | 说明 |
| --- | --- |
| `GET /opinions` | 日期范围、机构、品种、合约、周期及定义、方向、来源、复核状态、`cursor`、`limit` |
| `GET /opinions/{id}` | 观点详情与全部证据；不存在返回 404 |
| `GET /opinion-trends` | 必填 `product_id`、`period_key`、`from_date`、`to_date`；可选 `contract`、`source`、`period_definition` |
| `GET /evidence/{id}/source` | PDF／PNG 返回文件字节，HTML 返回文本摘录与小节 |

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/opinions?product_id=cu&period_key=short_term&from_date=2024-03-01&to_date=2024-03-31&limit=50'
Invoke-RestMethod 'http://127.0.0.1:8000/opinions/501'
Invoke-RestMethod 'http://127.0.0.1:8000/opinion-trends?product_id=cu&period_key=short_term&from_date=2024-03-01&to_date=2024-03-31'
Invoke-RestMethod 'http://127.0.0.1:8000/opinion-trends?...&source=llm'
```

列表响应：

```json
{
  "items": [
    {
      "id": 501,
      "institution": "宝城期货",
      "title": "有色早报（联调示例）",
      "published_on": "2024-03-01",
      "date_source": "publication",
      "product_id": "cu",
      "contract": "CU2404",
      "period_key": "short_term",
      "period_text": "短期",
      "period_definition": "<=1w",
      "direction": "bullish",
      "source": "rule",
      "statement_type": "unconditional",
      "review_state": "recognized"
    }
  ],
  "next_cursor": null
}
```

分页按 `sort_day DESC, id DESC`。下一页条件为
`sort_day < cursor_day OR (sort_day = cursor_day AND id < cursor_id)`，用
`limit + 1` 判断是否还有下一页。游标是 base64url 的
`{"d": 日期, "i": 观点 ID, "f": 筛选摘要}`，服务端校验类型、日期和筛选摘要；
筛选改变后旧游标返回 422 `cursor_invalid`，页面应从第一页重查。

趋势响应：

```json
{
  "source": "rule",
  "points": [
    {
      "date": "2024-03-01",
      "period_definition": "<=1w",
      "bullish_count": 1,
      "bearish_count": 0,
      "range_count": 0,
      "mixed_count": 0,
      "unresolved_count": 0,
      "conditional_count": 0,
      "report_count": 1,
      "institution_count": 1,
      "ratios": {"bullish": 1.0, "bearish": 0.0, "range": 0.0}
    }
  ]
}
```

统计口径：

1. 先按 `report_group_key`（机构 + 发布日 + 标题）去重，一份报告只算一票；
   同一报告同时有品种级判断和合约判断时优先品种级判断，只有各合约方向不同
   才计分歧。
2. 再按 `(发布日期, 机构, 期限定义)` 分桶：同机构同一天同一口径只算一次，
   方向相反计 `mixed_count`，不进多／空／震荡分母。
3. `ratios` 分母为 `bullish_count + bearish_count + range_count`，为 0 时三个值
   都是 `null`（页面显示「无明确观点」）。
4. `report_count` 是去重后的报告数，`institution_count` 是该日该系列纳入覆盖的
   机构数；两者都不能替代方向分母。
5. 条件观点只计 `conditional_count`，无方向记录计 `unresolved_count`。
   `source=rule` 与 `source=llm` 各自一套分母。
6. 查询排除 `review_state='review'` 与 `reports.metadata_review=1` 的记录，
   日期筛选天然排除发布日期未知的内部占位日期。

证据接口按证据保存的 `raw_article_id + content_hash` 调用
`load_article(article_id, content_hash)`，只返回该版本字节，不拼接 SQL、不把
HTML 当页面返回。PDF／PNG 带 `Content-Disposition: inline` 与
`X-Content-Type-Options: nosniff`。

错误响应统一为 `{"error": 错误码, "detail": 说明}`：

| 状态码 | 场景 |
| --- | --- |
| 422 | 日期区间颠倒、枚举非法、`limit` 超上限、游标损坏或筛选已变 |
| 404 | 观点、证据不存在，或证据原文版本不可读 |
| 503 | 未设置 `DB_PASSWORD`、驱动缺失、MySQL 连不上 |

## 编排流程

`analyze_article(article_id, dry_run=False)`：

1. 从 MySQL 读主文；建立 `analysis_attempts` 记录（预览模式跳过）。
2. `collect_report` 按 `article.format` 分流到 `extract_pdf`／`extract_html`／
   `extract_png`；解析 `attachment_refs`，按 `attachment_path` 归一化后查
   `find_article_id`，读取已入库附件并递归解析；缺失附件写
   `content_hash=null` 并记 `attachment_missing`；深度、数量有上限。
3. 汇总块并按 `input_parts`（role、路径、文章 ID、指纹）计算稳定 SHA-256。
4. `extract_metadata`、`extract_opinions`；给来自附件的证据补
   `attachment_chain`。
5. `plan_fallback`：只有 `no_explicit_view`、无方向、`statement_type=none`、
   `target_reliable` 且有可定位片段的范围才建任务，最多三条，按展望／逻辑／
   供需相关性和原文顺序选片。
6. 先查 `model_attempts` 缓存（文章、范围、片段指纹、模型、模板版本），命中
   直接复用已校验结果；否则 `infer_direction`，逐字校验引文后合并。
7. `save_opinions`：锁定参与分析的原文版本与报告行，核对指纹；按
   `(report_id, product_id, contract_key, period_key)` 更新观点、替换证据、
   删除本次结果已不存在的观点；`stale` 表示输入已变化。
8. 批处理逐篇输出 `saved`／`partial`／`stale`／`failed`，并带格式、范围数、
   模型调用数、可重试范围数、耗时，作为 1 号要求的回归记录。`partial` 表示
   观点已提交但仍有可重试的兜底范围。

## 与组内交付的衔接

- 阶段 02／03 的解析器：把交付的 `extract_pdf(article)`、`extract_png(article)`、
  `extract_html(article)` 覆盖 `backend/parsers/` 同名函数即可，返回
  `ExtractionResult(blocks, attachment_refs, issues)`。当前三个文件是按手册
  示例写的可运行占位实现（PDF 只取文字层，HTML 处理正文容器与表格，PNG 走
  Tesseract），表格版式与 OCR 复核以 2、3 号交付为准。
- 阶段 04 的规则与保存：`extract_metadata`、`extract_opinions` 在
  `backend/rules.py`，`save_opinions`、`begin_attempt` 等在
  `backend/analysis_store.py`。更换规则实现时保持函数签名与 `RuleResult`
  字段不变即可。
- 阶段 05 的兜底：`plan_fallback`、`infer_direction` 在 `backend/fallback.py`，
  已实现 SSE 解析、超时、字节上限、引文校验与 `retryable` 标记；接口地址与
  令牌从 `config.local.json` 的 `fallback` 段读取。
- 阶段 01 的导入：`backend/store.py` 由 1 号维护，`read_file`、`store_source`、
  `discover_files`、`detect_format` 直接复用；`backend/repository.py` 只做连接
  管理、异常转换和两条只读查询（`load_article`、`find_article_id`）。`store.py`
  的 `_connect()` 固定按环境变量建连接，为满足“仓库中不出现环境变量配置”，
  只读查询放在适配层；入库仍走 `store.store_source(connection, source)`，事务
  逻辑完全来自 `store.py`。

## 自测

```powershell
python -m unittest discover -s tests -t . -v
```

测试不连数据库、不调用外部命令：解析器用构造的 XHTML／TSV，模型用替身函数，
查询层直接对聚合函数断言。覆盖附件路径、指纹稳定性、兜底准入、SSE 断流、
引文改标点、游标与筛选绑定、同机构一票、分歧与条件观点口径等。依赖
`backend/store.py` 的用例在模块缺失时会自动跳过。

## 手册第 6 步验收对照

| 操作 | 代码位置 | 预期 |
| --- | --- | --- |
| 仅给文章 ID 分析宝城、大越 | `analyze` + `collect_report` | 从 MySQL 读文件，铜三周期与大越本周震荡可回查，模型调用 0 |
| 导入派生 PNG 再分析 | `ingest` + `parsers/png.py` | 格式 `png`，位置为原图像素 |
| 同一 ID `--dry-run` | `analyze_article(dry_run=True)` | 打印范围与拟兜底片段，不写分析号与观点，模型调用 0 |
| 模拟模型流中断重跑 | `fallback.read_sse` + 缓存 | 规则结果不丢，失败原因可查，重试只补未完成范围 |
| 分析时更换原文或附件 | `_lock_input_versions` | 旧运行标 `stale` 或保留上次成功快照 |
| 同日同机构重复报告 | `aggregate_trends` | 图上该口径一票，点击可回列表看多份报告 |
| 复用旧游标、访问不存在证据 | `decode_cursor`、证据接口 | 旧游标 422；无证据 404 |
