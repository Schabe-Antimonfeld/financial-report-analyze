"""HTML 正文、表格与附件解析。

使用标准库 ``html.parser`` 构建轻量 DOM，优先识别微信正文容器，再按通用
可读文本得分选择正文。标题、段落、表格单元和附件引用都保留文档顺序。
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

from backend.models import AttachmentRef, Block, ExtractionResult, Issue, RawArticle
from backend.textutil import normalize_text

BREAK = "\u2028"
VOID_TAGS = frozenset({"br", "img", "meta", "link", "input", "hr"})
REMOVED_TAGS = frozenset({"script", "style", "nav", "noscript"})
HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
BLOCK_TAGS = frozenset(
    {
        "p",
        "li",
        "div",
        "ul",
        "ol",
        "dl",
        "table",
        "section",
        "article",
        "main",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)
GENERAL_CONTAINER_TAGS = frozenset({"body", "div", "section"})
SECTION_TAGS = frozenset({"thead", "tbody", "tfoot"})
CELL_TAGS = frozenset({"td", "th"})
ATTACHMENT_SUFFIXES = (".pdf", ".html", ".htm", ".png")
INLINE_WHITESPACE = re.compile(r"[\t\n\r\f\v \u00a0]+")
BREAK_SPACES = re.compile(r" *\u2028 *")
MULTI_BREAKS = re.compile(r"\u2028+")
KNOWN_HEADING = re.compile(r"^已知品种[:：]$")
SHORT_HEADING = re.compile(r"^.{1,20}[:：]$")
CONTRACT = re.compile(r"[A-Za-z]{1,2}\d{4}")
CHARSET_VALUE = re.compile(
    r"""charset\s*=\s*["']?([^;"'\s]+)""",
    re.IGNORECASE,
)
ENCODING_ALIASES = {
    "gbk": "gb18030",
    "gb2312": "gb18030",
    "gb_2312-80": "gb18030",
    "gb-2312": "gb18030",
    "x-gbk": "gb18030",
    "gb18030": "gb18030",
}
BOM_ENCODINGS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xff\xfe", "utf-16-le"),
)


class EncodingMetaParser(HTMLParser):
    """从 ``meta`` 标签读取 HTML 编码声明。"""

    def __init__(self) -> None:
        """初始化解析器。"""
        super().__init__(convert_charrefs=True)
        self.encoding = ""

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """处理普通开始标签。

        :param tag: 标签名。
        :param attrs: 属性列表。
        :return: ``None``。
        """
        if tag.lower() != "meta" or self.encoding:
            return
        values = {name.lower(): (value or "") for name, value in attrs}
        charset = values.get("charset", "").strip()
        if charset:
            self.encoding = charset
            return
        http_equiv = values.get("http-equiv", "").strip().lower()
        if http_equiv != "content-type":
            return
        match = CHARSET_VALUE.search(values.get("content", ""))
        if match:
            self.encoding = match.group(1)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """处理自闭合标签。

        :param tag: 标签名。
        :param attrs: 属性列表。
        :return: ``None``。
        """
        self.handle_starttag(tag, attrs)


def _normalize_encoding(label: str) -> str:
    """规范化常见中文编码别名。

    :param label: 页面声明的编码名。
    :return: Python 可用的编码名。
    """
    cleaned = label.strip().strip("\"'").lower()
    return ENCODING_ALIASES.get(cleaned, cleaned)


def _declared_encoding(content: bytes) -> str:
    """扫描字节内容中的编码声明。

    :param content: HTML 原始字节。
    :return: 声明的编码；没有声明时返回空字符串。
    """
    parser = EncodingMetaParser()
    try:
        parser.feed(content.decode("latin-1"))
        parser.close()
    except (AssertionError, ValueError):
        return ""
    return parser.encoding


def decode_html(content: bytes) -> tuple[str, str]:
    """识别并解码 HTML 字节。

    依次尝试 BOM、页面声明编码、UTF-8 和 GB18030，全部采用严格解码。

    :param content: HTML 原始字节。
    :return: ``(文本, 实际使用的编码名)``。
    :raises ValueError: 所有候选编码均无法严格解码时抛出 ``encoding_error``。
    """
    for bom, encoding in BOM_ENCODINGS:
        if not content.startswith(bom):
            continue
        try:
            text = content.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            break
        if text.startswith("\ufeff"):
            text = text[1:]
        return text, encoding

    declared = _normalize_encoding(_declared_encoding(content))
    candidates: list[str] = []
    for encoding in (declared, "utf-8", "gb18030"):
        if encoding and encoding not in candidates:
            candidates.append(encoding)
    for encoding in candidates:
        try:
            return content.decode(encoding, errors="strict"), encoding
        except (LookupError, UnicodeDecodeError):
            continue
    raise ValueError("encoding_error")


@dataclass
class _DomNode:
    """轻量 DOM 节点。"""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_DomNode] = field(default_factory=list)
    text: str = ""
    parent: _DomNode | None = None
    index: int = 0
    parts: list[str | _DomNode] = field(default_factory=list)


class _DomBuilder(HTMLParser):
    """把 HTML 构建成带文档序号的轻量 DOM。"""

    def __init__(self) -> None:
        """初始化文档根与解析栈。"""
        super().__init__(convert_charrefs=True)
        self.root = _DomNode(tag="#document")
        self._stack = [self.root]
        self._next_index = 1

    @property
    def _current(self) -> _DomNode:
        """当前接收文本的节点。"""
        return self._stack[-1]

    def _find_open(self, tags: set[str], stop: set[str] | None = None) -> int:
        """在解析栈中查找最近的指定标签。

        :param tags: 要查找的标签集合。
        :param stop: 遇到即停止查找的边界标签集合。
        :return: 栈下标；未找到时返回 ``-1``。
        """
        boundaries = stop or set()
        for index in range(len(self._stack) - 1, 0, -1):
            tag = self._stack[index].tag
            if tag in tags:
                return index
            if tag in boundaries:
                return -1
        return -1

    def _close_optional(self, tag: str) -> None:
        """按常见可省略结束标签规则关闭旧节点。

        :param tag: 即将开始的新标签。
        :return: ``None``。
        """
        index = -1
        if tag == "p":
            index = self._find_open({"p"})
        elif tag == "li":
            index = self._find_open({"li"}, {"ul", "ol", "menu"})
        elif tag in CELL_TAGS:
            index = self._find_open(CELL_TAGS, {"tr", "table"})
        elif tag == "tr":
            index = self._find_open({"tr"}, {"table"})
        if index > 0:
            del self._stack[index:]

    def _open_tag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        push: bool,
    ) -> None:
        """创建元素节点并按需入栈。

        :param tag: 标签名。
        :param attrs: 属性列表。
        :param push: 是否作为后续文本的当前节点。
        :return: ``None``。
        """
        name = tag.lower()
        self._close_optional(name)
        attributes = {
            key.lower(): (value if value is not None else "")
            for key, value in attrs
        }
        node = _DomNode(
            tag=name,
            attrs=attributes,
            parent=self._current,
            index=self._next_index,
        )
        self._next_index += 1
        self._current.children.append(node)
        self._current.parts.append(node)
        if push and name not in VOID_TAGS:
            self._stack.append(node)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """处理开始标签。

        :param tag: 标签名。
        :param attrs: 属性列表。
        :return: ``None``。
        """
        self._open_tag(tag, attrs, push=True)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """处理自闭合标签。

        :param tag: 标签名。
        :param attrs: 属性列表。
        :return: ``None``。
        """
        self._open_tag(tag, attrs, push=False)

    def handle_endtag(self, tag: str) -> None:
        """处理结束标签并关闭对应节点。

        :param tag: 标签名。
        :return: ``None``。
        """
        name = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == name:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """把文本挂到当前节点。

        :param data: 文本片段。
        :return: ``None``。
        """
        if not data:
            return
        self._current.text += data
        self._current.parts.append(data)


def _build_dom(text: str) -> _DomNode:
    """解析 HTML 文本并返回文档根。

    :param text: 已解码的 HTML。
    :return: 轻量 DOM 根节点。
    """
    builder = _DomBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


def _is_hidden(node: _DomNode) -> bool:
    """判断元素是否被隐藏。

    :param node: DOM 节点。
    :return: 含 ``hidden`` 或隐藏样式时为 ``True``。
    """
    if "hidden" in node.attrs:
        return True
    style = re.sub(r"\s+", "", node.attrs.get("style", "").lower())
    return "display:none" in style or "visibility:hidden" in style


def _prune(node: _DomNode) -> None:
    """移除脚本、导航和隐藏节点。

    :param node: 当前节点。
    :return: ``None``。
    """
    kept: list[_DomNode] = []
    for child in node.children:
        if child.tag in REMOVED_TAGS or _is_hidden(child):
            continue
        _prune(child)
        kept.append(child)
    kept_ids = {id(child) for child in kept}
    node.children = kept
    node.parts = [
        part
        for part in node.parts
        if isinstance(part, str) or id(part) in kept_ids
    ]


def _iter_elements(node: _DomNode) -> Iterator[_DomNode]:
    """按文档顺序遍历后代元素。

    :param node: 起始节点。
    :return: 元素迭代器。
    """
    for child in node.children:
        yield child
        yield from _iter_elements(child)


def _raw_text(node: _DomNode) -> str:
    """读取节点内全部文本，并把 ``br`` 标记为换行。

    :param node: DOM 节点。
    :return: 未清理的文本。
    """
    pieces: list[str] = []

    def visit(current: _DomNode) -> None:
        """递归收集节点及其子节点文本。

        :param current: 当前节点。
        :return: ``None``。
        """
        for part in current.parts:
            if isinstance(part, str):
                pieces.append(part)
            elif part.tag == "br":
                pieces.append(BREAK)
            else:
                visit(part)

    visit(node)
    return "".join(pieces)


def _clean_text(raw: str) -> str:
    """压缩空白并保留 ``br`` 产生的显式换行。

    :param raw: 原始文本。
    :return: 清理后的文本。
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = INLINE_WHITESPACE.sub(" ", text)
    text = BREAK_SPACES.sub(BREAK, text)
    text = MULTI_BREAKS.sub(BREAK, text)
    return text.strip(" \u2028")


def _text_with_breaks(node: _DomNode) -> str:
    """取得节点文本，保留显式换行分隔符。

    :param node: DOM 节点。
    :return: 清理后的文本。
    """
    return _clean_text(_raw_text(node))


def _inline_text(node: _DomNode) -> str:
    """取得用于标题、链接和表格的同行文本。

    :param node: DOM 节点。
    :return: 换行被压成空格的文本。
    """
    return _text_with_breaks(node).replace(BREAK, " ").strip()


def _score(node: _DomNode) -> int:
    """计算候选正文容器的可读文本得分。

    :param node: 候选节点。
    :return: 可见文本长度减去链接文本长度。
    """
    visible = len(_inline_text(node))
    anchor_length = sum(
        len(_inline_text(element))
        for element in _iter_elements(node)
        if element.tag == "a"
    )
    return visible - anchor_length


def _best_by_score(candidates: list[_DomNode]) -> _DomNode | None:
    """按得分选择候选，平分时取文档序更早者。

    :param candidates: 候选节点。
    :return: 最佳候选；列表为空时返回 ``None``。
    """
    readable = [node for node in candidates if _score(node) > 0]
    if not readable:
        return None
    return max(readable, key=lambda node: (_score(node), -node.index))


def _choose_container(root: _DomNode) -> _DomNode | None:
    """选择报告正文容器。

    :param root: 文档根。
    :return: 正文容器；没有可读正文时返回 ``None``。
    """
    elements = list(_iter_elements(root))
    for element in elements:
        if element.attrs.get("id", "").strip() == "js_content":
            if _score(element) > 0:
                return element
    for element in elements:
        classes = element.attrs.get("class", "").lower()
        if "rich_media_content" in classes and _score(element) > 0:
            return element
    for tag in ("main", "article"):
        candidates = [element for element in elements if element.tag == tag]
        selected = _best_by_score(candidates)
        if selected is not None:
            return selected
    general = [
        element
        for element in elements
        if element.tag in GENERAL_CONTAINER_TAGS
    ]
    selected = _best_by_score(general)
    if selected is not None:
        return selected
    if _score(root) > 0:
        return root
    return None


def _is_leaf_div(node: _DomNode) -> bool:
    """判断 div 是否只包含行内内容。

    :param node: DOM 节点。
    :return: 没有块级后代时为 ``True``。
    """
    return not any(
        element.tag in BLOCK_TAGS for element in _iter_elements(node)
    )


def _span(node: _DomNode, name: str, *, allow_zero: bool = False) -> int:
    """读取并限制表格跨行跨列数。

    :param node: 单元格节点。
    :param name: ``rowspan`` 或 ``colspan``。
    :param allow_zero: 是否保留 ``rowspan=0`` 的特殊含义。
    :return: 1 到 100 之间的跨度。
    """
    raw = node.attrs.get(name, "1").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    if value == 0:
        return 0 if allow_zero else 1
    if value < 0:
        return 1
    return min(value, 100)


@dataclass
class _TableRow:
    """表格中的一行及其行组编号。"""

    node: _DomNode
    group: int


@dataclass
class _CellSpec:
    """原始单元格及其跨行跨列信息。"""

    node: _DomNode
    row: int
    rowspan: int
    colspan: int


@dataclass
class _PlacedCell:
    """已确定逻辑坐标的单元格。"""

    spec: _CellSpec
    row: int
    col: int


def _collect_rows(table: _DomNode) -> list[_TableRow]:
    """收集表格行，并给每个行组编号。

    :param table: ``table`` 节点。
    :return: 按文档顺序排列的行。
    """
    rows: list[_TableRow] = []
    group_counter = [0]

    def visit(node: _DomNode, group: int) -> None:
        """递归收集行，并传递当前行组编号。

        :param node: 当前节点。
        :param group: 当前行组编号。
        :return: ``None``。
        """
        for child in node.children:
            if child.tag == "tr":
                rows.append(_TableRow(child, group))
            elif child.tag in SECTION_TAGS:
                group_counter[0] += 1
                visit(child, group_counter[0])
            elif child.tag == "table":
                continue
            else:
                visit(child, group)

    visit(table, 0)
    return rows


def _collect_cells(row: _DomNode) -> list[_DomNode]:
    """收集一行中的原始单元格，忽略嵌套表格。

    :param row: ``tr`` 节点。
    :return: 单元格节点列表。
    """
    cells: list[_DomNode] = []

    def visit(node: _DomNode) -> None:
        """递归收集单元格，跳过嵌套表格。

        :param node: 当前节点。
        :return: ``None``。
        """
        for child in node.children:
            if child.tag in CELL_TAGS:
                cells.append(child)
            elif child.tag == "table":
                continue
            else:
                visit(child)

    visit(row)
    return cells


def _place_cells(
    table: _DomNode,
    issues: list[Issue],
) -> tuple[list[_PlacedCell], int]:
    """按跨行跨列把单元格放入逻辑网格。

    :param table: ``table`` 节点。
    :param issues: 用于追加重叠问题的列表。
    :return: 已定位单元格和网格列数。
    """
    rows = _collect_rows(table)
    if not rows:
        return [], 0
    row_count = len(rows)
    group_end: dict[int, int] = {}
    for index, row in enumerate(rows):
        group_end[row.group] = index

    specs: list[_CellSpec] = []
    for row_index, row in enumerate(rows):
        for cell in _collect_cells(row.node):
            rowspan = _span(cell, "rowspan", allow_zero=True)
            if rowspan == 0:
                rowspan = group_end[row.group] - row_index + 1
            rowspan = max(1, min(rowspan, row_count - row_index, 100))
            colspan = max(1, min(_span(cell, "colspan"), 100))
            specs.append(_CellSpec(cell, row_index, rowspan, colspan))

    occupied: dict[tuple[int, int], int] = {}
    placements: list[_PlacedCell] = []
    width = 0
    for spec in specs:
        col = 0
        while (spec.row, col) in occupied:
            col += 1
        coordinates = [
            (row, column)
            for row in range(spec.row, spec.row + spec.rowspan)
            for column in range(col, col + spec.colspan)
        ]
        overlap = any(coordinate in occupied for coordinate in coordinates)
        if overlap:
            issues.append(
                Issue(
                    "layout_review",
                    None,
                    f"表格单元格重叠：第 {spec.row + 1} 行第 {col + 1} 列，"
                    "已保留单元格文本",
                )
            )
        for coordinate in coordinates:
            occupied.setdefault(coordinate, spec.node.index)
        placements.append(_PlacedCell(spec, spec.row, col))
        width = max(width, col + spec.colspan)
    return placements, width


def _contract_text(row_label: str | None) -> str | None:
    """从行标签中提取合约代码。

    :param row_label: 行标签文本。
    :return: 匹配到的合约文本；没有时返回 ``None``。
    """
    if not row_label:
        return None
    match = CONTRACT.search(row_label)
    return match.group(0) if match else None


def _table_blocks(
    table: _DomNode,
    article: RawArticle,
    heading_path: list[str],
    issues: list[Issue],
) -> list[Block]:
    """把一张表转换为表格单元块。

    :param table: ``table`` 节点。
    :param article: 当前原文。
    :param heading_path: 表格所在的小节路径。
    :param issues: 用于追加版面问题的列表。
    :return: 按原始单元格顺序排列的块。
    """
    placements, _width = _place_cells(table, issues)
    if not placements:
        return []
    by_row: dict[int, list[_PlacedCell]] = {}
    for placement in placements:
        by_row.setdefault(placement.row, []).append(placement)

    header_rows = 0
    for row_index in sorted(by_row):
        row_cells = by_row[row_index]
        if row_cells and all(
            placement.spec.node.tag == "th" for placement in row_cells
        ):
            header_rows += 1
        else:
            break

    blocks: list[Block] = []
    for placement in placements:
        cell = placement.spec.node
        row = placement.row
        col = placement.col
        headers: list[str] = []
        seen_headers: set[int] = set()
        limit = min(header_rows, row + 1)
        for header_row in range(limit):
            for header_cell in by_row.get(header_row, []):
                if not header_cell.col <= col < header_cell.col + header_cell.spec.colspan:
                    continue
                if id(header_cell.spec.node) in seen_headers:
                    continue
                seen_headers.add(id(header_cell.spec.node))
                header_text = _inline_text(header_cell.spec.node)
                if header_text:
                    headers.append(header_text)

        row_label: str | None = None
        if row >= header_rows:
            for row_cell in sorted(by_row.get(row, []), key=lambda item: item.col):
                scope = row_cell.spec.node.attrs.get("scope", "").lower()
                if row_cell.spec.node.tag == "th" or scope == "row":
                    row_label = _inline_text(row_cell.spec.node) or None
                    break
        text = _inline_text(cell)
        table_data = {
            "row": row + 1,
            "col": col + 1,
            "headers": headers,
            "row_label": row_label,
            "contract_text": _contract_text(row_label),
        }
        blocks.append(
            Block(
                block_id="",
                order=0,
                raw_article_id=article.article_id,
                content_hash=article.content_hash,
                kind="table_cell",
                text=text,
                heading_path=list(heading_path),
                location={"format": "html", "element_index": cell.index},
                table=table_data,
            )
        )
    return blocks


def _is_heading_line(line: str) -> bool:
    """判断无 ``p`` 正文中的短行是否为标题行。

    :param line: 单行文本。
    :return: 是已知品种行或短冒号结尾行时为 ``True``。
    """
    return bool(KNOWN_HEADING.match(line) or SHORT_HEADING.match(line))


def _is_attachment(href: str, attrs: dict[str, str]) -> bool:
    """判断链接是否应作为附件引用。

    :param href: 原始链接。
    :param attrs: 链接属性。
    :return: 扩展名匹配或带 ``download`` 时为 ``True``。
    """
    if "download" in attrs:
        return True
    path = href.split("#", 1)[0].split("?", 1)[0].strip().lower()
    return path.endswith(ATTACHMENT_SUFFIXES)


class _HTMLEmitter:
    """按文档顺序生成正文块。"""

    def __init__(self, article: RawArticle, container: _DomNode) -> None:
        """初始化生成器。

        :param article: 当前原文。
        :param container: 已选正文容器。
        """
        self.article = article
        self.container = container
        self.blocks: list[Block] = []
        self.issues: list[Issue] = []
        self._heading_stack: list[tuple[int, str]] = []

    def _heading_path(self) -> list[str]:
        """取得当前小节路径。

        :return: 标题文本列表。
        """
        return [text for _level, text in self._heading_stack]

    def _apply_heading(self, level: int, text: str) -> None:
        """把标题压入路径栈并弹出同级或更低层级。

        :param level: 标题级别。
        :param text: 标题文本。
        :return: ``None``。
        """
        while self._heading_stack and self._heading_stack[-1][0] >= level:
            self._heading_stack.pop()
        self._heading_stack.append((level, text))

    def _new_block(
        self,
        kind: str,
        text: str,
        path: list[str],
        node: _DomNode,
    ) -> Block:
        """构造正文块。

        :param kind: 块类型。
        :param text: 块文本。
        :param path: 小节路径快照。
        :param node: 来源元素。
        :return: 正文块。
        """
        return Block(
            block_id="",
            order=0,
            raw_article_id=self.article.article_id,
            content_hash=self.article.content_hash,
            kind=kind,
            text=text,
            heading_path=list(path),
            location={"format": "html", "element_index": node.index},
        )

    def _is_candidate(self, node: _DomNode) -> bool:
        """判断节点本身是否可以产出段落块。

        :param node: DOM 节点。
        :return: 应尝试产出段落时为 ``True``。
        """
        if node.tag in {"p", "li"}:
            return True
        if node.tag == "div":
            return _is_leaf_div(node)
        if node is self.container and node.tag in {
            "body",
            "main",
            "article",
            "section",
            "#document",
        }:
            return not any(
                element.tag in BLOCK_TAGS for element in _iter_elements(node)
            )
        return False

    def _line_blocks(
        self,
        node: _DomNode,
        text: str,
        path: list[str],
    ) -> list[Block]:
        """把含显式换行的 div 文本拆成逐行块。

        :param node: 来源 div。
        :param text: 含换行分隔符的文本。
        :param path: 进入 div 时的小节路径。
        :return: 标题行或段落行块。
        """
        blocks: list[Block] = []
        current_path = list(path)
        for line in text.split(BREAK):
            line = line.strip()
            if not line:
                continue
            if _is_heading_line(line):
                self._apply_heading(1, line)
                current_path = self._heading_path()
                blocks.append(
                    self._new_block("heading", line, current_path, node)
                )
                continue
            blocks.append(self._new_block("paragraph", line, current_path, node))
        return blocks

    def _node_blocks(
        self,
        node: _DomNode,
        text: str,
        path: list[str],
    ) -> list[Block]:
        """根据节点自身文本生成块。

        :param node: 来源节点。
        :param text: 节点文本。
        :param path: 进入节点时的小节路径。
        :return: 节点自身产生的块。
        """
        if node.tag == "div":
            return self._line_blocks(node, text, path)
        paragraph = text.replace(BREAK, "\n").strip()
        if not paragraph:
            return []
        return [self._new_block("paragraph", paragraph, path, node)]

    def _collect_blocks(self, node: _DomNode) -> list[Block]:
        """递归收集节点及其后代的块。

        :param node: DOM 节点。
        :return: 文档顺序块列表。
        """
        path = self._heading_path()
        if node.tag == "table":
            return _table_blocks(node, self.article, path, self.issues)
        if node.tag in HEADING_TAGS:
            text = _inline_text(node)
            if not text:
                return []
            level = int(node.tag[1])
            self._apply_heading(level, text)
            return [self._new_block("heading", text, self._heading_path(), node)]

        child_blocks: list[Block] = []
        for child in node.children:
            child_blocks.extend(self._collect_blocks(child))
        text = _text_with_breaks(node)
        if not text or not self._is_candidate(node):
            return child_blocks
        parent_text = normalize_text(text.replace(BREAK, " "))
        child_text = normalize_text("".join(block.text for block in child_blocks))
        if child_blocks and parent_text == child_text:
            return child_blocks
        return self._node_blocks(node, text, path) + child_blocks

    def run(self) -> list[Block]:
        """执行整棵正文树的块提取。

        :return: 正文块列表。
        """
        return self._collect_blocks(self.container)


def _collect_attachments(container: _DomNode) -> list[AttachmentRef]:
    """收集正文容器内的附件引用。

    :param container: 正文容器。
    :return: 按文档顺序排列的附件引用。
    """
    refs: list[AttachmentRef] = []
    for element in _iter_elements(container):
        if element.tag != "a":
            continue
        href = element.attrs.get("href", "").strip()
        if not href or not _is_attachment(href, element.attrs):
            continue
        refs.append(
            AttachmentRef(
                href=href,
                label=_inline_text(element),
                location={
                    "format": "html",
                    "element_index": element.index,
                },
            )
        )
    return refs


def extract_html(article: RawArticle) -> ExtractionResult:
    """提取 HTML 正文块、表格单元和附件引用。

    :param article: 含 HTML 字节的原文。
    :return: 块、附件引用与问题列表。
    """
    try:
        text, _encoding = decode_html(article.content)
    except ValueError:
        return ExtractionResult(
            issues=[Issue("encoding_error", None, "HTML 编码识别失败")]
        )

    root = _build_dom(text)
    _prune(root)
    container = _choose_container(root)
    if container is None:
        return ExtractionResult(
            issues=[Issue("body_missing", None, "No readable report body")]
        )
    emitter = _HTMLEmitter(article, container)
    blocks = emitter.run()
    if not blocks:
        return ExtractionResult(
            attachments=_collect_attachments(container),
            issues=[Issue("body_missing", None, "No readable report body")],
        )
    return ExtractionResult(
        blocks=blocks,
        attachment_refs=_collect_attachments(container),
        issues=emitter.issues,
    )
