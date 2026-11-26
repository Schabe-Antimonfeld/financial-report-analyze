"""规则层基线：元数据补全与基于词表的方向识别。

本模块只依赖 ``models`` 与 ``textutil``。``extract_metadata`` 从元数据块
和标题补全机构、标题与发布日期；``extract_opinions`` 依次应用表格单元
格、正文分句与标题规则，合并同一（品种, 合约, 周期）范围，并为没有方向
的正文范围留下 ``no_explicit_view`` 兜底候选，供后续模型兜底使用。
"""
from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass, field
from typing import Any

from backend.errors import (
    REASON_DIRECTION_CONFLICT,
    REASON_NO_EXPLICIT_VIEW,
    REASON_TARGET_REVIEW,
)
from backend.models import (
    Block,
    Evidence,
    Issue,
    RawArticle,
    ReportMetadata,
    RuleResult,
    ScopeResult,
)
from backend.textutil import normalize_text

# 机构后缀、块类型与规则常量。
INSTITUTION_SUFFIXES = ("期货", "证券", "银行", "研究院")
METADATA_KIND = "metadata"
HEADING_KIND = "heading"
TABLE_CELL_KIND = "table_cell"
REPORT_KINDS = (METADATA_KIND, HEADING_KIND)
REASON_NEGATED_VIEW = "negated_view"
REASON_METADATA_REVIEW = "metadata_review"

# 方向词表：匹配时按长度降序，保证「震荡偏强」优先于「震荡」。
DIRECTIONS = {
    "震荡偏强": "bullish",
    "预计上涨": "bullish",
    "上涨": "bullish",
    "偏强": "bullish",
    "看涨": "bullish",
    "看多": "bullish",
    "震荡偏弱": "bearish",
    "预计下跌": "bearish",
    "下跌": "bearish",
    "偏弱": "bearish",
    "看跌": "bearish",
    "看空": "bearish",
    "维持震荡": "range",
    "区间震荡": "range",
    "横盘": "range",
    "震荡": "range",
    "震荡运行": "range",
}
DIRECTION_PHRASES = sorted(DIRECTIONS, key=lambda item: (-len(item), item))
DIRECTION_PATTERN = re.compile("|".join(re.escape(phrase) for phrase in DIRECTION_PHRASES))

# 品种词表：别名按长度降序匹配，代码用于合约规范化。
PRODUCTS: list[dict[str, Any]] = [
    {
        "product_id": "cu",
        "name": "铜",
        "aliases": ["沪铜", "铜"],
        "exchange": "SHFE",
        "code": "CU",
    },
    {
        "product_id": "iron_ore",
        "name": "铁矿石",
        "aliases": ["铁矿石", "铁矿", "连铁"],
        "exchange": "DCE",
        "code": "I",
    },
    {
        "product_id": "zn",
        "name": "锌",
        "aliases": ["沪锌", "锌"],
        "exchange": "SHFE",
        "code": "ZN",
    },
    {
        "product_id": "al",
        "name": "铝",
        "aliases": ["沪铝", "铝"],
        "exchange": "SHFE",
        "code": "AL",
    },
    {
        "product_id": "au",
        "name": "黄金",
        "aliases": ["沪金", "黄金"],
        "exchange": "SHFE",
        "code": "AU",
    },
    {
        "product_id": "ag",
        "name": "白银",
        "aliases": ["沪银", "白银"],
        "exchange": "SHFE",
        "code": "AG",
    },
    {
        "product_id": "rb",
        "name": "螺纹钢",
        "aliases": ["螺纹钢", "螺纹"],
        "exchange": "SHFE",
        "code": "RB",
    },
]
PRODUCT_BY_ID = {product["product_id"]: product for product in PRODUCTS}
PRODUCT_BY_CODE = {product["code"]: product for product in PRODUCTS}
PRODUCT_ALIASES: list[tuple[str, str]] = sorted(
    (
        (alias, product["product_id"])
        for product in PRODUCTS
        for alias in product["aliases"]
    ),
    key=lambda item: (-len(item[0]), item[0]),
)

# 周期词表与期限定义。
PERIOD_WORDS = {
    "日内": "intraday",
    "日间": "intraday",
    "短期": "short_term",
    "中期": "medium_term",
    "长期": "long_term",
    "本周": "week:this",
    "下周": "week:next",
}
PERIOD_DEFINITIONS = {
    "intraday": "当天",
    "short_term": "<=1w",
    "medium_term": "2w-1mo",
    "long_term": ">1mo",
    "week:this": "本周",
    "week:next": "下周",
    "unspecified": None,
}
PERIOD_MATCH_ORDER = sorted(PERIOD_WORDS, key=lambda item: (-len(item), item))

# 合约、日期与句式模式。
CONTRACT_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,2})(\d{4})(?![0-9])")
DATE_PATTERN = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?(?!\d)"
)
SENTENCE_SPLIT = re.compile(r"[。！？；!?;\n]+")
COMMA_SPLIT = re.compile(r"[，,]+")
MOVEMENT_PATTERN = re.compile(r"上涨|下跌|走强|走弱|反弹|回落|大涨|大跌|涨|跌")
STRATEGY_PATTERN = re.compile(
    r"逢高(?:抛空|做空|沽空)|逢低(?:做多|买入|吸纳)|"
    r"区间操作|高抛低吸|止损|止盈|观望|轻仓"
)
NEGATION_PATTERN = re.compile(
    r"不看(?:涨|跌|多|空|好)|不看好|"
    r"不(?:会|太|宜)(?:上涨|下跌|看涨|看跌|看多|看空)|"
    r"未必(?:会)?(?:上涨|下跌|涨|跌)|难以(?:上涨|下跌)|"
    r"不(?:上涨|下跌)"
)

# 语境、时态与等级标记。
OUTLOOK_MARKERS = (
    "预计",
    "展望",
    "认为",
    "维持",
    "有望",
    "料",
    "将",
    "判断",
    "偏",
    "看",
    "震荡",
    "上涨",
    "下跌",
)
FUTURE_MARKERS = ("预计", "将", "展望", "认为", "有望", "料")
PAST_MARKERS = ("昨日", "上一交易日", "此前")
CONDITIONAL_MARKERS = ("若", "如果", "只有", "除非")
PARAGRAPH_GRADE_MARKERS = ("后市", "展望", "核心逻辑")
FALLBACK_KEYWORDS = ("预计", "展望", "核心逻辑", "供需", "库存")
GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}
MAX_EVIDENCE = 3


@dataclass
class _Segment:
    """一个用于匹配的文本片段。

    :param quote: 原文中的精确子串，用于证据引文。
    :param normalized: 匹配副本。
    """

    quote: str
    normalized: str


@dataclass
class _Candidate:
    """分句级方向候选。

    :param direction: 方向或 ``None``。
    :param grade: 证据等级 A/B/C/D。
    :param order: 首块阅读顺序，用于稳定排序。
    :param evidence: 该候选携带的证据。
    :param statement_type: 陈述类型。
    :param review_state: 覆盖默认复核状态。
    :param reason_code: 覆盖默认原因码。
    :param conditions: 条件分支。
    """

    direction: str | None
    grade: str
    order: int
    evidence: list[Evidence] = field(default_factory=list)
    statement_type: str = "unconditional"
    review_state: str | None = None
    reason_code: str | None = None
    conditions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Branch:
    """条件句的一个方向分支。

    :param product_id: 品种。
    :param contract: 规范合约或 ``None``。
    :param period_key: 周期键。
    :param period_text: 周期原文。
    :param period_definition: 期限定义。
    :param direction: 分支方向。
    :param quote: 分支原文。
    :param grade: 证据等级。
    """

    product_id: str
    contract: str | None
    period_key: str
    period_text: str | None
    period_definition: str | None
    direction: str
    quote: str
    grade: str


@dataclass
class _ScopeBucket:
    """同一 scope_id 下的候选、附带证据与兜底块。

    :param product_id: 品种。
    :param contract: 规范合约或 ``None``。
    :param period_key: 周期键。
    :param period_text: 周期原文。
    :param period_definition: 期限定义。
    :param target_reliable: 目标是否可靠。
    :param creation: 创建序号，用于稳定排序。
    :param candidates: 方向候选。
    :param attachments: 操作建议等附带证据。
    :param fallback_only: 是否仅为兜底候选。
    :param fallback_blocks: 兜底原文块。
    """

    product_id: str
    contract: str | None
    period_key: str
    period_text: str | None = None
    period_definition: str | None = None
    target_reliable: bool = True
    creation: int = 0
    candidates: list[_Candidate] = field(default_factory=list)
    attachments: list[Evidence] = field(default_factory=list)
    fallback_only: bool = False
    fallback_blocks: list[Block] = field(default_factory=list)

    @property
    def scope_id(self) -> str:
        """返回业务键字符串。

        :return: ``品种|合约|周期``。
        """
        return _scope_id(self.product_id, self.contract, self.period_key)


class _RuleState:
    """规则提取过程中的可变状态。"""

    def __init__(self) -> None:
        """初始化空状态。"""
        self.buckets: dict[str, _ScopeBucket] = {}
        self.issues: list[Issue] = []
        self.consumed_blocks: set[str] = set()
        self.issue_keys: set[tuple[str, str]] = set()
        self.creation = 0

    def bucket(
        self,
        product_id: str,
        contract: str | None,
        period_key: str,
        period_text: str | None,
        period_definition: str | None,
    ) -> _ScopeBucket:
        """取得或创建范围桶。

        :param product_id: 品种。
        :param contract: 规范合约或 ``None``。
        :param period_key: 周期键。
        :param period_text: 周期原文。
        :param period_definition: 期限定义。
        :return: 已存在的或新建的范围桶。
        """
        key = _scope_id(product_id, contract, period_key)
        bucket = self.buckets.get(key)
        if bucket is None:
            self.creation += 1
            bucket = _ScopeBucket(
                product_id=product_id,
                contract=contract,
                period_key=period_key,
                period_text=period_text,
                period_definition=period_definition,
                creation=self.creation,
            )
            self.buckets[key] = bucket
        elif bucket.period_text is None:
            bucket.period_text = period_text
        return bucket

    def add_issue(self, code: str, detail: str) -> None:
        """按（代码, 说明）去重追加问题。

        :param code: 问题代码。
        :param detail: 中文说明。
        :return: ``None``。
        """
        key = (code, detail)
        if key in self.issue_keys:
            return
        self.issue_keys.add(key)
        self.issues.append(Issue(code=code, page=None, detail=detail))


def extract_metadata(article: RawArticle, blocks: list[Block]) -> ReportMetadata:
    """从元数据块与标题补全报告元数据。

    :param article: 原文记录，提供采集日期与机构提示。
    :param blocks: 解析得到的块，按 ``order`` 排序后使用。
    :return: 不虚构日期或机构的报告元数据。
    """
    ordered = sorted(blocks, key=lambda block: block.order)
    metadata = ReportMetadata(collected_on=article.collected_on)
    sources: dict[str, Any] = {}
    issues: list[Issue] = []
    hint = (article.institution_hint or "").strip()
    institution, institution_block, institution_values = _find_institution(
        ordered,
        article,
    )
    if institution:
        metadata.institution = institution
        if institution_block is not None:
            sources["institution"] = {"block_id": institution_block.block_id}
        else:
            sources["institution"] = {"source": "institution_hint"}
    if len(institution_values) > 1:
        detail = "机构表述不一致：" + "、".join(institution_values)
        issues.append(Issue(code=REASON_METADATA_REVIEW, page=None, detail=detail))
    if hint and institution_values and not any(
        hint in value or value in hint for value in institution_values
    ):
        detail = f"机构提示与正文不一致：{hint} / {institution_values[0]}"
        issues.append(Issue(code=REASON_METADATA_REVIEW, page=None, detail=detail))

    # 标题优先取第一个标题块，否则退回文件名主干。
    heading_blocks = [
        block for block in ordered if block.kind == HEADING_KIND and block.text.strip()
    ]
    filename_title = _filename_stem(article.relative_path)
    title_text = ""
    if heading_blocks:
        first_heading = heading_blocks[0]
        title_text = first_heading.text.strip()
        metadata.title = title_text
        sources["title"] = {"block_id": first_heading.block_id}
    elif filename_title:
        title_text = filename_title
        metadata.title = title_text
        sources["title"] = {"source": "filename"}

    # 只看前几个标题或元数据块和标题，避免把正文历史日期当成发布日。
    date_candidates: list[tuple[datetime.date, dict[str, Any]]] = []
    for block in ordered[:5]:
        if block.kind not in REPORT_KINDS:
            continue
        for value in _find_dates(block.text):
            date_candidates.append((value, {"block_id": block.block_id}))
    if title_text:
        for value in _find_dates(title_text):
            date_candidates.append((value, dict(sources.get("title") or {})))
    if date_candidates:
        published = min(item[0] for item in date_candidates)
        metadata.published_on = published
        metadata.date_source = "publication"
        for value, source in date_candidates:
            if value == published:
                sources["published_on"] = source
                break
        distinct = sorted({item[0] for item in date_candidates})
        if len(distinct) > 1:
            detail = "发布日期不一致：" + "、".join(item.isoformat() for item in distinct)
            issues.append(Issue(code=REASON_METADATA_REVIEW, page=None, detail=detail))
    elif article.collected_on is not None:
        metadata.published_on = article.collected_on
        metadata.date_source = "directory"
        sources["published_on"] = {"source": "directory"}

    metadata.issues = issues
    metadata.field_sources = sources
    return metadata


def extract_opinions(blocks: list[Block], metadata: ReportMetadata) -> RuleResult:
    """按规则识别各（品种, 合约, 周期）范围的观点。

    :param blocks: 解析得到的块，按 ``order`` 顺序处理。
    :param metadata: 报告元数据，暂不参与方向识别，保留以固定接口。
    :return: 范围结果与问题列表。
    """
    # 元数据不改变方向判定，规则层只依赖块本身的阅读顺序与出处。
    _ = metadata
    ordered = sorted(blocks, key=lambda block: block.order)
    state = _RuleState()
    for block in ordered:
        if block.kind == TABLE_CELL_KIND:
            if block.table:
                _apply_table_cell(block, state)
            continue
        if block.kind == METADATA_KIND or not block.text.strip():
            continue
        _apply_text_block(block, state)
    _apply_fallback(ordered, state)
    block_orders = {block.block_id: block.order for block in ordered}
    scopes = _finalize(state, block_orders)
    for scope in scopes:
        if scope.reason_code == REASON_DIRECTION_CONFLICT:
            state.add_issue(
                REASON_DIRECTION_CONFLICT,
                f"{scope.scope_id} 存在同级相反方向",
            )
    return RuleResult(scopes=scopes, issues=state.issues)


def direction_matches(clause: str) -> list[tuple[str, str]]:
    """按最长优先匹配方向词。

    :param clause: 已规范化的分句或原文。
    :return: ``[(词, 方向)]``，按出现顺序排列。
    """
    return [
        (match.group(0), DIRECTIONS[match.group(0)])
        for match in DIRECTION_PATTERN.finditer(clause)
    ]


def _apply_table_cell(block: Block, state: _RuleState) -> None:
    """规则 A：从表格单元格提取明确观点。

    :param block: ``table_cell`` 块。
    :param state: 规则状态。
    :return: ``None``。
    """
    table = block.table or {}
    headers = table.get("headers") or []
    row_label = normalize_text(str(table.get("row_label") or ""))
    product_id = _match_product(row_label)
    raw_contract = str(table.get("contract_text") or "")
    contract = _normalize_contract(raw_contract, product_id)
    if contract is None and row_label:
        contract = _contract_from_row_label(row_label, product_id)
    prefix_product = _contract_product(contract)
    if product_id is None:
        product_id = prefix_product
    if product_id is None:
        return
    if prefix_product is not None and prefix_product != product_id:
        contract = None
    header_text = ""
    if headers:
        raw_header = headers[-1]
        if isinstance(raw_header, (list, tuple)):
            raw_header = raw_header[-1] if raw_header else ""
        header_text = str(raw_header or "").strip()
    period_key, _word = _period_pair(header_text) or ("unspecified", None)
    period_definition = table.get("period_definition")
    if not period_definition:
        period_definition = PERIOD_DEFINITIONS.get(period_key)
    matches = direction_matches(block.normalized or normalize_text(block.text))
    if not matches:
        return
    bucket = state.bucket(
        product_id,
        contract,
        period_key,
        header_text or None,
        period_definition,
    )
    bucket.target_reliable = True
    evidence = _evidence(
        block,
        block.text,
        "outlook",
        "table_direction",
        "A",
        contract,
    )
    bucket.candidates.append(
        _Candidate(
            direction=matches[0][1],
            grade="A",
            order=block.order,
            evidence=[evidence],
        )
    )
    state.consumed_blocks.add(block.block_id)


def _apply_text_block(block: Block, state: _RuleState) -> None:
    """规则 B/C/D：对正文或标题逐句提取方向候选。

    :param block: 正文、标题或其他文本块。
    :param state: 规则状态。
    :return: ``None``。
    """
    heading_product = _heading_product(block.heading_path)
    heading_period = _heading_period(block.heading_path)
    is_heading = block.kind == HEADING_KIND
    grade = _block_grade(block, is_heading, heading_product is not None)
    branches: dict[str, list[_Branch]] = {}
    for sentence in SENTENCE_SPLIT.split(block.text):
        quote = sentence.strip()
        if not quote:
            continue
        normalized = normalize_text(quote)
        conditional = _has_conditional(normalized) and bool(direction_matches(normalized))
        if conditional and not NEGATION_PATTERN.search(normalized):
            _collect_branch(
                block,
                quote,
                normalized,
                heading_product,
                heading_period,
                grade,
                is_heading,
                branches,
                state,
            )
            continue
        for piece in COMMA_SPLIT.split(quote):
            segment_quote = piece.strip()
            if not segment_quote:
                continue
            segment = _Segment(
                quote=segment_quote,
                normalized=normalize_text(segment_quote),
            )
            _apply_segment(
                block,
                segment,
                heading_product,
                heading_period,
                grade,
                is_heading,
                state,
            )
    _emit_branches(block, branches, state)


def _apply_segment(
    block: Block,
    segment: _Segment,
    heading_product: str | None,
    heading_period: tuple[str, str] | None,
    grade: str,
    is_heading: bool,
    state: _RuleState,
) -> None:
    """处理一个普通分句（规则 B/C/D）。

    :param block: 所属块。
    :param segment: 分句。
    :param heading_product: 标题路径中的品种。
    :param heading_period: 标题路径中的周期。
    :param grade: 等级。
    :param is_heading: 是否标题块。
    :param state: 规则状态。
    :return: ``None``。
    """
    text = segment.normalized
    if _is_past_background(text):
        return
    contract = _find_contract(text)
    product_id = _contract_product(contract) or _match_product(text) or heading_product
    period_key, period_text = _resolve_period(text, heading_period)
    period_definition = PERIOD_DEFINITIONS.get(period_key)
    matches = direction_matches(text)
    if NEGATION_PATTERN.search(text):
        if product_id is None:
            if not is_heading:
                state.add_issue(
                    REASON_TARGET_REVIEW,
                    f"方向表述缺少品种：{segment.quote[:40]}",
                )
            return
        bucket = state.bucket(
            product_id,
            contract,
            period_key,
            period_text,
            period_definition,
        )
        bucket.target_reliable = True
        evidence = _evidence(
            block,
            segment.quote,
            "outlook",
            "negated_view",
            grade,
            contract,
        )
        bucket.candidates.append(
            _Candidate(
                direction=None,
                grade=grade,
                order=block.order,
                evidence=[evidence],
                statement_type="none",
                review_state="review",
                reason_code=REASON_NEGATED_VIEW,
            )
        )
        state.consumed_blocks.add(block.block_id)
        return
    if not matches:
        if STRATEGY_PATTERN.search(text) and contract is not None:
            _attach_strategy(
                block,
                segment,
                contract,
                period_key,
                period_text,
                grade,
                state,
            )
        return
    if not _has_outlook_context(text):
        return
    if product_id is None:
        if not is_heading:
            state.add_issue(
                REASON_TARGET_REVIEW,
                f"方向表述缺少品种：{segment.quote[:40]}",
            )
        return
    bucket = state.bucket(
        product_id,
        contract,
        period_key,
        period_text,
        period_definition,
    )
    bucket.target_reliable = True
    evidence = _evidence(
        block,
        segment.quote,
        "outlook",
        "direction_phrase",
        grade,
        contract,
    )
    bucket.candidates.append(
        _Candidate(
            direction=matches[0][1],
            grade=grade,
            order=block.order,
            evidence=[evidence],
        )
    )
    state.consumed_blocks.add(block.block_id)


def _collect_branch(
    block: Block,
    quote: str,
    normalized: str,
    heading_product: str | None,
    heading_period: tuple[str, str] | None,
    grade: str,
    is_heading: bool,
    branches: dict[str, list[_Branch]],
    state: _RuleState,
) -> None:
    """把一个条件句登记为待合并分支。

    :param block: 所属块。
    :param quote: 原文分句。
    :param normalized: 匹配副本。
    :param heading_product: 标题路径中的品种。
    :param heading_period: 标题路径中的周期。
    :param grade: 等级。
    :param is_heading: 是否标题块。
    :param branches: 按范围键收集的分支。
    :param state: 规则状态。
    :return: ``None``。
    """
    matches = direction_matches(normalized)
    if not matches:
        return
    contract = _find_contract(normalized)
    product_id = _contract_product(contract) or _match_product(normalized)
    if product_id is None:
        product_id = heading_product
    if product_id is None:
        if not is_heading:
            state.add_issue(
                REASON_TARGET_REVIEW,
                f"方向表述缺少品种：{quote[:40]}",
            )
        return
    period_key, period_text = _resolve_period(normalized, heading_period)
    branch = _Branch(
        product_id=product_id,
        contract=contract,
        period_key=period_key,
        period_text=period_text,
        period_definition=PERIOD_DEFINITIONS.get(period_key),
        direction=matches[0][1],
        quote=quote,
        grade=grade,
    )
    key = _scope_id(product_id, contract, period_key)
    branches.setdefault(key, []).append(branch)


def _emit_branches(
    block: Block,
    branches: dict[str, list[_Branch]],
    state: _RuleState,
) -> None:
    """合并同一范围的条件分支，必要时生成情景范围。

    :param block: 所属块。
    :param branches: 按范围键收集的分支。
    :param state: 规则状态。
    :return: ``None``。
    """
    for items in branches.values():
        first = items[0]
        bucket = state.bucket(
            first.product_id,
            first.contract,
            first.period_key,
            first.period_text,
            first.period_definition,
        )
        bucket.target_reliable = True
        directions = {item.direction for item in items}
        if len(directions) > 1:
            conditions = [
                {
                    "condition": item.quote,
                    "direction": item.direction,
                    "contract": item.contract,
                    "block_id": block.block_id,
                }
                for item in items
            ]
            evidence = [
                _evidence(
                    block,
                    item.quote,
                    "outlook",
                    "conditional_branch",
                    item.grade,
                    item.contract,
                )
                for item in items
            ]
            bucket.candidates.append(
                _Candidate(
                    direction=None,
                    grade=first.grade,
                    order=block.order,
                    evidence=evidence,
                    statement_type="scenario",
                    conditions=conditions,
                )
            )
        else:
            for item in items:
                evidence = _evidence(
                    block,
                    item.quote,
                    "outlook",
                    "conditional_branch",
                    item.grade,
                    item.contract,
                )
                bucket.candidates.append(
                    _Candidate(
                        direction=item.direction,
                        grade=item.grade,
                        order=block.order,
                        evidence=[evidence],
                        statement_type="conditional",
                    )
                )
        state.consumed_blocks.add(block.block_id)


def _attach_strategy(
    block: Block,
    segment: _Segment,
    contract: str,
    period_key: str,
    period_text: str | None,
    grade: str,
    state: _RuleState,
) -> None:
    """把操作建议挂到同品种最近的范围上。

    :param block: 所属块。
    :param segment: 分句。
    :param contract: 规范合约。
    :param period_key: 周期键。
    :param period_text: 周期原文。
    :param grade: 等级。
    :param state: 规则状态。
    :return: ``None``。
    """
    product_id = _contract_product(contract)
    if product_id is None:
        return
    related = [
        bucket for bucket in state.buckets.values() if bucket.product_id == product_id
    ]
    target: _ScopeBucket | None = None
    for bucket in reversed(related):
        if bucket.contract == contract:
            target = bucket
            break
    if target is None:
        for bucket in reversed(related):
            if bucket.contract is None:
                target = bucket
                break
    if target is None and related:
        target = related[-1]
    if target is None:
        target = state.bucket(
            product_id,
            contract,
            period_key,
            period_text,
            PERIOD_DEFINITIONS.get(period_key),
        )
        target.target_reliable = False
    target.attachments.append(
        _evidence(
            block,
            segment.quote,
            "strategy",
            "strategy_advice",
            grade,
            contract,
        )
    )
    state.consumed_blocks.add(block.block_id)


def _apply_fallback(blocks: list[Block], state: _RuleState) -> None:
    """为没有方向的正文品种范围生成兜底候选。

    :param blocks: 按 ``order`` 排序的块。
    :param state: 规则状态。
    :return: ``None``。
    """
    grouped: dict[str, list[Block]] = {}
    keys: dict[str, tuple[str, str]] = {}
    for block in blocks:
        if block.kind in (METADATA_KIND, HEADING_KIND, TABLE_CELL_KIND):
            continue
        if not block.text.strip():
            continue
        product_id = _heading_product(block.heading_path)
        if product_id is None:
            continue
        if block.block_id in state.consumed_blocks:
            continue
        heading_period = _heading_period(block.heading_path)
        period_key, _period_text = heading_period or ("unspecified", None)
        key = _scope_id(product_id, None, period_key)
        if key in state.buckets:
            continue
        keys.setdefault(key, (product_id, period_key))
        grouped.setdefault(key, []).append(block)
    for key, group_blocks in grouped.items():
        if not group_blocks:
            continue
        product_id, period_key = keys[key]
        fallback_blocks = _pick_fallback_blocks(blocks, product_id)
        if not fallback_blocks:
            continue
        bucket = state.bucket(
            product_id,
            None,
            period_key,
            None,
            PERIOD_DEFINITIONS.get(period_key),
        )
        bucket.fallback_only = True
        bucket.target_reliable = True
        bucket.fallback_blocks = fallback_blocks


def _finalize(
    state: _RuleState,
    block_orders: dict[str, int],
) -> list[ScopeResult]:
    """构建范围结果并按证据块顺序稳定排序。

    :param state: 规则状态。
    :param block_orders: 块 ID 到阅读顺序的映射。
    :return: 稳定排序后的范围结果。
    """
    recognized: list[tuple[int, int, ScopeResult]] = []
    fallback: list[tuple[int, int, ScopeResult]] = []
    for bucket in state.buckets.values():
        scope = _build_scope(bucket)
        if bucket.fallback_only:
            order = min(
                (block.order for block in bucket.fallback_blocks),
                default=0,
            )
            fallback.append((order, bucket.creation, scope))
            continue
        orders = [block_orders.get(item.block_id, 0) for item in scope.evidence]
        order = min(orders) if orders else 0
        recognized.append((order, bucket.creation, scope))
    recognized.sort(key=lambda item: (item[0], item[1]))
    fallback.sort(key=lambda item: (item[0], item[1]))
    return [scope for _order, _creation, scope in recognized + fallback]


def _build_scope(bucket: _ScopeBucket) -> ScopeResult:
    """把一个范围桶合并为最终范围结果。

    :param bucket: 范围桶。
    :return: 范围结果。
    """
    scope = ScopeResult(
        scope_id=bucket.scope_id,
        product_id=bucket.product_id,
        contract=bucket.contract,
        period_key=bucket.period_key,
        period_text=bucket.period_text,
        period_definition=bucket.period_definition,
        target_reliable=bucket.target_reliable,
    )
    if bucket.fallback_only:
        scope.source = None
        scope.statement_type = "none"
        scope.review_state = "unresolved"
        scope.reason_code = REASON_NO_EXPLICIT_VIEW
        scope.fallback_blocks = list(bucket.fallback_blocks)
        if bucket.fallback_blocks:
            scope.outlook_text = bucket.fallback_blocks[0].text
        return scope
    has_higher = any(
        GRADE_RANK.get(item.grade, 9) < GRADE_RANK["D"]
        for item in bucket.candidates
    )
    plain = [
        item
        for item in bucket.candidates
        if item.statement_type == "unconditional" and item.direction is not None
    ]
    conditional = [
        item
        for item in bucket.candidates
        if item.statement_type == "conditional" and item.direction is not None
    ]
    if has_higher:
        plain = [item for item in plain if item.grade != "D"]
        conditional = [item for item in conditional if item.grade != "D"]
    scenarios = [
        item for item in bucket.candidates if item.statement_type == "scenario"
    ]
    negated = [
        item
        for item in bucket.candidates
        if item.reason_code == REASON_NEGATED_VIEW
    ]
    pool = plain or conditional
    statement = "unconditional" if plain else "conditional"
    if pool:
        direction, top, conflicted = _resolve_pool(pool)
        if conflicted:
            scope.source = "rule"
            scope.statement_type = statement
            scope.review_state = "review"
            scope.reason_code = REASON_DIRECTION_CONFLICT
            scope.outlook_text = _first_quote(top)
            evidence = _candidate_evidence(top)
            scope.evidence = _attach_evidence(evidence, bucket.attachments)
            return scope
        chosen = [item for item in pool if item.direction == direction]
        chosen.sort(
            key=lambda item: (GRADE_RANK.get(item.grade, 9), item.order)
        )
        scope.direction = direction
        scope.source = "rule"
        scope.statement_type = statement
        scope.review_state = "recognized"
        scope.outlook_text = _first_quote(chosen)
        evidence = _candidate_evidence(chosen)
        evidence.extend(_reason_evidence(bucket.candidates, direction))
        scope.evidence = _attach_evidence(evidence, bucket.attachments)
        return scope
    if scenarios:
        conditions: list[dict[str, Any]] = []
        for candidate in scenarios:
            conditions.extend(candidate.conditions)
        scope.source = "rule"
        scope.statement_type = "scenario"
        scope.review_state = "unresolved"
        scope.conditions = conditions
        scope.outlook_text = _first_quote(scenarios)
        evidence = _candidate_evidence(scenarios)
        scope.evidence = _attach_evidence(evidence, bucket.attachments)
        return scope
    if negated:
        scope.source = "rule"
        scope.statement_type = "none"
        scope.review_state = "review"
        scope.reason_code = REASON_NEGATED_VIEW
        scope.outlook_text = _first_quote(negated)
        evidence = _candidate_evidence(negated)
        scope.evidence = _attach_evidence(evidence, bucket.attachments)
        return scope
    scope.source = None
    scope.statement_type = "none"
    scope.review_state = "unresolved"
    evidence = _attach_evidence([], bucket.attachments)
    scope.evidence = evidence
    scope.outlook_text = evidence[0].quote if evidence else None
    return scope


def _resolve_pool(
    pool: list[_Candidate],
) -> tuple[str | None, list[_Candidate], bool]:
    """返回最高等级候选、方向与同级冲突标记。

    :param pool: 同一类型的候选。
    :return: ``（方向, 最高等级候选, 是否同级冲突）``。
    """
    ordered = sorted(
        pool,
        key=lambda item: (GRADE_RANK.get(item.grade, 9), item.order),
    )
    best_grade = ordered[0].grade
    top = [item for item in ordered if item.grade == best_grade]
    directions = {
        item.direction for item in top if item.direction is not None
    }
    if len(directions) > 1:
        return None, top, True
    direction = directions.pop() if directions else None
    return direction, top, False


def _candidate_evidence(
    candidates: list[_Candidate],
    *,
    limit: int = MAX_EVIDENCE,
) -> list[Evidence]:
    """按等级与块顺序收集候选证据并去重。

    :param candidates: 候选列表。
    :param limit: 最多保留的证据数。
    :return: 去重后的证据。
    """
    ordered = sorted(
        candidates,
        key=lambda item: (GRADE_RANK.get(item.grade, 9), item.order),
    )
    result: list[Evidence] = []
    seen: set[tuple[str, str]] = set()
    for candidate in ordered:
        for item in candidate.evidence:
            key = (item.block_id, item.quote)
            if key in seen:
                continue
            if len(result) >= limit:
                return result
            seen.add(key)
            result.append(item)
    return result


def _attach_evidence(
    evidence: list[Evidence],
    attachments: list[Evidence],
    *,
    limit: int = 2,
) -> list[Evidence]:
    """把操作建议等附带证据追加到证据列表。

    :param evidence: 已有证据。
    :param attachments: 附带证据。
    :param limit: 最多追加的附带证据数。
    :return: 追加后的证据列表。
    """
    seen = {(item.block_id, item.quote) for item in evidence}
    added = 0
    for item in attachments:
        if added >= limit:
            break
        key = (item.block_id, item.quote)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(item)
        added += 1
    return evidence


def _reason_evidence(
    candidates: list[_Candidate],
    direction: str | None,
) -> list[Evidence]:
    """把低等级相反方向改记为理由证据。

    :param candidates: 全部候选。
    :param direction: 已确定的方向。
    :return: 种类为 ``reason`` 的证据，最多两条。
    """
    result: list[Evidence] = []
    ordered = sorted(
        candidates,
        key=lambda item: (GRADE_RANK.get(item.grade, 9), item.order),
    )
    for candidate in ordered:
        if candidate.direction is None or candidate.direction == direction:
            continue
        for item in candidate.evidence:
            result.append(_copy_evidence(item, "reason"))
        if len(result) >= 2:
            break
    return result[:2]


def _first_quote(candidates: list[_Candidate]) -> str | None:
    """取候选中的第一条引文。

    :param candidates: 候选列表。
    :return: 引文或 ``None``。
    """
    for candidate in candidates:
        if candidate.evidence:
            return candidate.evidence[0].quote
    return None


def _copy_evidence(evidence: Evidence, kind: str) -> Evidence:
    """复制证据并替换种类。

    :param evidence: 原证据。
    :param kind: 新种类。
    :return: 新证据。
    """
    return Evidence(
        kind=kind,
        quote=evidence.quote,
        block_id=evidence.block_id,
        raw_article_id=evidence.raw_article_id,
        content_hash=evidence.content_hash,
        location=dict(evidence.location),
        rule_id=evidence.rule_id,
        grade=evidence.grade,
        contract=evidence.contract,
        attachment_chain=list(evidence.attachment_chain),
    )


def _evidence(
    block: Block,
    quote: str,
    kind: str,
    rule_id: str,
    grade: str,
    contract: str | None,
) -> Evidence:
    """构造带出处和标题路径的证据。

    :param block: 来源块。
    :param quote: 原文引文。
    :param kind: 证据种类。
    :param rule_id: 规则标识。
    :param grade: 证据等级。
    :param contract: 规范合约。
    :return: 证据对象。
    """
    location = dict(block.location)
    location["heading_path"] = list(block.heading_path)
    return Evidence(
        kind=kind,
        quote=quote,
        block_id=block.block_id,
        raw_article_id=block.raw_article_id,
        content_hash=block.content_hash,
        location=location,
        rule_id=rule_id,
        grade=grade,
        contract=contract,
    )


def _block_grade(block: Block, is_heading: bool, has_product_heading: bool) -> str:
    """按块类型与段落特征给出规则等级。

    :param block: 文本块。
    :param is_heading: 是否标题块。
    :param has_product_heading: 标题路径是否含品种。
    :return: ``A``/``B``/``C``/``D``。
    """
    if is_heading:
        return "D"
    if has_product_heading and any(
        marker in block.text for marker in PARAGRAPH_GRADE_MARKERS
    ):
        return "B"
    return "C"


def _is_past_background(text: str) -> bool:
    """判断分句是否只是过去行情回顾。

    :param text: 匹配副本。
    :return: 无未来标记且含过去标记与涨跌词时为 ``True``。
    """
    if any(marker in text for marker in FUTURE_MARKERS):
        return False
    if not any(marker in text for marker in PAST_MARKERS):
        return False
    return MOVEMENT_PATTERN.search(text) is not None


def _has_outlook_context(text: str) -> bool:
    """判断分句是否包含展望语境词。

    :param text: 匹配副本。
    :return: 命中任一展望词时为 ``True``。
    """
    return any(marker in text for marker in OUTLOOK_MARKERS)


def _has_conditional(text: str) -> bool:
    """判断分句是否含条件标记。

    :param text: 匹配副本。
    :return: 命中任一条件词时为 ``True``。
    """
    return any(marker in text for marker in CONDITIONAL_MARKERS)


def _resolve_period(
    text: str,
    heading_period: tuple[str, str] | None,
) -> tuple[str, str | None]:
    """按分句、标题、未指定顺序确定周期。

    :param text: 分句匹配副本。
    :param heading_period: 标题路径中的周期。
    :return: ``（周期键, 周期原文）``。
    """
    explicit = _period_pair(text)
    if explicit is not None:
        return explicit
    if heading_period is not None:
        return heading_period
    return "unspecified", None


def _period_pair(text: str) -> tuple[str, str] | None:
    """返回文本中优先级最高的周期词及其键。

    :param text: 任意文本。
    :return: ``（周期键, 原文词）`` 或 ``None``。
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    best: tuple[int, int, str] | None = None
    for word in PERIOD_MATCH_ORDER:
        index = normalized.find(word)
        if index < 0:
            continue
        if best is None or (index, -len(word)) < best[:2]:
            best = (index, -len(word), word)
    if best is None:
        return None
    word = best[2]
    return PERIOD_WORDS[word], word


def _match_product(text: str) -> str | None:
    """按别名在文本中的最先出现位置识别品种。

    :param text: 已规范化的文本。
    :return: 品种 ID 或 ``None``。
    """
    if not text:
        return None
    best: tuple[int, int, str] | None = None
    for alias, product_id in PRODUCT_ALIASES:
        index = text.find(alias)
        if index < 0:
            continue
        if best is None or (index, -len(alias)) < best[:2]:
            best = (index, -len(alias), product_id)
    return best[2] if best is not None else None


def _find_contract(text: str) -> str | None:
    """从文本中提取并规范第一个有效合约。

    :param text: 分句匹配副本。
    :return: 规范合约或 ``None``。
    """
    return _normalize_contract(text, None)


def _normalize_contract(value: str, product_id: str | None) -> str | None:
    """把合约写法规范为大写品种代码加四位月份。

    :param value: 表格合约文本或正文片段。
    :param product_id: 已知品种，用于补全纯数字合约。
    :return: 规范合约或 ``None``。
    """
    text = normalize_text(value)
    if not text:
        return None
    for match in CONTRACT_PATTERN.finditer(text):
        prefix = match.group(1).upper()
        digits = match.group(2)
        if prefix not in PRODUCT_BY_CODE:
            continue
        if not 1 <= int(digits[-2:]) <= 12:
            continue
        return f"{prefix}{digits}"
    bare = re.fullmatch(r"(\d{4})", text)
    if bare is None or product_id is None:
        return None
    digits = bare.group(1)
    if not 1 <= int(digits[-2:]) <= 12:
        return None
    code = str(PRODUCT_BY_ID[product_id]["code"])
    return f"{code}{digits}"


def _contract_from_row_label(row_label: str, product_id: str | None) -> str | None:
    """从品种列文本中提取合约。

    宝城样本把品种和四位合约号放在同一格，例如「铜2404」「铜 2404」；
    也兼容「CU2404」这种带代码前缀的写法。

    :param row_label: 表格行标签原文。
    :param product_id: 已识别品种，用于补全纯数字合约。
    :return: 规范合约或 ``None``。
    """
    contract = _find_contract(row_label)
    if contract is not None:
        return contract
    if product_id is None:
        return None
    matched = re.search(r"(?<!\d)(\d{4})(?!\d)", row_label)
    if matched is None:
        return None
    return _normalize_contract(matched.group(1), product_id)


def _contract_product(contract: str | None) -> str | None:
    """由规范合约代码反查品种。

    :param contract: 规范合约或 ``None``。
    :return: 品种 ID 或 ``None``。
    """
    if not contract:
        return None
    prefix = contract[:-4].upper()
    for product in PRODUCTS:
        if str(product["code"]) == prefix:
            return str(product["product_id"])
    return None


def _heading_product(heading_path: list[str]) -> str | None:
    """从标题路径最内层向外查找品种。

    :param heading_path: 标题路径。
    :return: 品种 ID 或 ``None``。
    """
    for heading in reversed(heading_path):
        product_id = _match_product(normalize_text(heading))
        if product_id is not None:
            return product_id
    return None


def _heading_period(heading_path: list[str]) -> tuple[str, str] | None:
    """从标题路径最内层向外查找周期。

    :param heading_path: 标题路径。
    :return: ``（周期键, 原文词）`` 或 ``None``。
    """
    for heading in reversed(heading_path):
        period = _period_pair(heading)
        if period is not None:
            return period
    return None


def _pick_fallback_blocks(blocks: list[Block], product_id: str) -> list[Block]:
    """挑选同一品种标题下最多三个兜底块。

    :param blocks: 全部块。
    :param product_id: 品种 ID。
    :return: 兜底块，含完整原文。
    """
    candidates: list[tuple[int, int, Block]] = []
    for block in blocks:
        if block.kind in (METADATA_KIND, HEADING_KIND, TABLE_CELL_KIND):
            continue
        if not block.text.strip():
            continue
        if _heading_product(block.heading_path) != product_id:
            continue
        priority = 0 if any(word in block.text for word in FALLBACK_KEYWORDS) else 1
        candidates.append((priority, block.order, block))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [block for _priority, _order, block in candidates[:MAX_EVIDENCE]]


def _scope_id(product_id: str, contract: str | None, period_key: str) -> str:
    """拼接（品种, 合约, 周期）业务键。

    :param product_id: 品种。
    :param contract: 规范合约或 ``None``。
    :param period_key: 周期键。
    :return: ``品种|合约|周期``。
    """
    return f"{product_id}|{contract or ''}|{period_key}"


def _find_institution(
    blocks: list[Block],
    article: RawArticle,
) -> tuple[str | None, Block | None, list[str]]:
    """查找机构名并返回题名块与全部候选。

    :param blocks: 按 ``order`` 排序的块。
    :param article: 原文记录。
    :return: ``（机构名, 来源块, 全部候选文本）``。
    """
    matches: list[Block] = []
    for block in blocks:
        if block.kind != METADATA_KIND:
            continue
        text = block.text.strip()
        if text and any(suffix in text for suffix in INSTITUTION_SUFFIXES):
            matches.append(block)
    if matches:
        values = [item.text.strip() for item in matches]
        return values[0], matches[0], values
    hint = (article.institution_hint or "").strip()
    if hint:
        return hint, None, []
    return None, None, []


def _filename_stem(relative_path: str) -> str:
    """取相对路径的文件名主干。

    :param relative_path: 原文相对路径。
    :return: 不含扩展名的文件名。
    """
    name = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    stem, _extension = os.path.splitext(name)
    return stem.strip()


def _find_dates(text: str) -> list[datetime.date]:
    """提取文本中形如发布日期的日期。

    :param text: 任意文本。
    :return: 合法日期列表。
    """
    results: list[datetime.date] = []
    for match in DATE_PATTERN.finditer(text):
        year, month, day = (int(part) for part in match.groups())
        if not 1990 <= year <= 2100:
            continue
        try:
            value = datetime.date(year, month, day)
        except ValueError:
            continue
        results.append(value)
    return results
