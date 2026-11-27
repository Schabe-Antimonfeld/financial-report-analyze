"""格式解析器单元测试。

覆盖 HTML 编码识别、正文容器、标题路径、表格网格和附件引用，并用
伪造的 pdftotext XHTML、Tesseract TSV 输出覆盖 PDF 与 PNG 的坐标和
错误分支。测试不依赖 Poppler 或 Tesseract 可执行文件。
"""
from __future__ import annotations

import codecs
import struct
import subprocess
import unittest
from unittest.mock import patch

from backend.models import RawArticle
from backend.parsers.html import decode_html, extract_html
from backend.parsers.pdf import (
    PDF_LAYOUT_COMMAND,
    PDF_TEXT_COMMAND,
    extract_pdf,
)
from backend.parsers.png import TESSERACT_COMMAND, extract_png


def _article(content: bytes, file_format: str) -> RawArticle:
    """构造测试用原文。

    :param content: 原始字节。
    :param file_format: ``html``、``pdf`` 或 ``png``。
    :return: 原文对象。
    """
    return RawArticle(
        article_id=7,
        relative_path=f"reports/sample.{file_format}",
        content_hash="hash-7",
        format=file_format,
        content=content,
    )


def _completed(stdout: bytes) -> subprocess.CompletedProcess[bytes]:
    """构造成功的子进程结果。

    :param stdout: 标准输出。
    :return: 子进程结果。
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=stdout,
        stderr=b"",
    )


def _pdf_xhtml() -> bytes:
    """构造两页的 bbox-layout XHTML。

    :return: XHTML 字节。
    """
    return """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="doc">
<page width="600" height="800">
<flow><block>
<line xMin="10" yMin="20" xMax="140" yMax="35">
<word xMin="10" yMin="20" xMax="40" yMax="35">Hello</word>
<word xMin="45" yMin="20" xMax="80" yMax="35">world</word>
<word xMin="85" yMin="20" xMax="110" yMax="35">沪铜</word>
<word xMin="112" yMin="20" xMax="140" yMax="35">2404</word>
</line>
<line xMin="20" yMin="50" xMax="80" yMax="70">
<word xMin="20" yMin="50" xMax="80" yMax="70">第二行</word>
</line>
</block></flow>
</page>
<page width="400" height="500">
<flow><block>
<line xMin="5" yMin="10" xMax="60" yMax="30">
<word xMin="5" yMin="10" xMax="60" yMax="30">风险提示</word>
</line>
</block></flow>
</page>
</div>
</body>
</html>""".encode("utf-8")


def _png_bytes(width: int = 640, height: int = 480) -> bytes:
    """构造只含签名和 IHDR 的 PNG 头部。

    :param width: 图片宽。
    :param height: 图片高。
    :return: 可被校验函数接受的 PNG 字节。
    """
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def _tesseract_tsv() -> bytes:
    """构造带两行和低置信度词的 Tesseract TSV。

    :return: UTF-8 TSV 字节。
    """
    header = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
    )
    rows = [
        "5\t1\t1\t1\t1\t1\t10\t20\t30\t20\t95\t沪\n",
        "5\t1\t1\t1\t1\t2\t40\t20\t20\t20\t90\t铜\n",
        "5\t1\t1\t1\t2\t1\t12\t50\t40\t20\t45\t看多\n",
        "5\t2\t1\t1\t1\t1\t5\t8\t30\t12\t99\t风险\n",
    ]
    return (header + "".join(rows)).encode("utf-8")


class HTMLDecodeTests(unittest.TestCase):
    """HTML 编码识别测试。"""

    def test_utf8_without_meta(self) -> None:
        """无声明时按 UTF-8 解码。"""
        source = "<html><body>中文正文</body></html>"
        text, encoding = decode_html(source.encode("utf-8"))
        self.assertEqual(text, source)
        self.assertEqual(encoding, "utf-8")

    def test_gb18030_with_meta_charset(self) -> None:
        """meta charset 的 GB2312 别名映射到 GB18030。"""
        source = '<html><head><meta charset="gb2312"></head><body>中文正文</body></html>'
        text, encoding = decode_html(source.encode("gb18030"))
        self.assertEqual(text, source)
        self.assertEqual(encoding, "gb18030")

    def test_gb18030_with_http_equiv(self) -> None:
        """http-equiv Content-Type 同样能识别中文编码。"""
        source = (
            '<html><head><meta http-equiv="Content-Type" '
            'content="text/html; charset=gb_2312-80"></head>'
            "<body>期货早报</body></html>"
        )
        text, encoding = decode_html(source.encode("gb18030"))
        self.assertEqual(text, source)
        self.assertEqual(encoding, "gb18030")

    def test_utf8_bom(self) -> None:
        """UTF-8 BOM 优先于页面声明。"""
        source = "<html><body>带 BOM 的正文</body></html>"
        content = codecs.BOM_UTF8 + source.encode("utf-8")
        text, encoding = decode_html(content)
        self.assertEqual(text, source)
        self.assertEqual(encoding, "utf-8-sig")

    def test_utf16_bom(self) -> None:
        """UTF-16 LE BOM 能被正确剥离和识别。"""
        source = "<html><body>UTF16 正文</body></html>"
        content = codecs.BOM_UTF16_LE + source.encode("utf-16-le")
        text, encoding = decode_html(content)
        self.assertEqual(text, source)
        self.assertEqual(encoding, "utf-16-le")

    def test_all_encodings_fail(self) -> None:
        """非法字节在全部严格解码失败后抛出 encoding_error。"""
        content = b"<html><body>" + bytes([0x81]) + b"</body></html>"
        with self.assertRaisesRegex(ValueError, "encoding_error"):
            decode_html(content)


class HTMLExtractTests(unittest.TestCase):
    """HTML 正文、表格和附件测试。"""

    def test_container_selection_removes_nav_and_hidden(self) -> None:
        """优先正文容器，并去掉导航、脚本和隐藏节点。"""
        source = (
            "<html><body>"
            "<nav><p>导航链接</p></nav>"
            '<div class="rich_media_content article">'
            "<p>第一段正文</p>"
            '<p style="display: none">隐藏段</p>'
            "<p>第二段正文</p>"
            "</div>"
            "<script>忽略脚本</script>"
            "</body></html>"
        )
        result = extract_html(_article(source.encode("utf-8"), "html"))
        self.assertEqual([block.text for block in result.blocks], ["第一段正文", "第二段正文"])
        self.assertEqual(result.issues, [])

    def test_heading_path(self) -> None:
        """语义标题弹出同级或更低层级并维护路径。"""
        source = (
            "<html><body><article>"
            "<h1>一、总览</h1><p>甲</p>"
            "<h2>铜</h2><p>乙</p>"
            "<h1>二、风险</h1><p>丙</p>"
            "</article></body></html>"
        )
        result = extract_html(_article(source.encode("utf-8"), "html"))
        self.assertEqual(
            [(block.kind, block.text, block.heading_path) for block in result.blocks],
            [
                ("heading", "一、总览", ["一、总览"]),
                ("paragraph", "甲", ["一、总览"]),
                ("heading", "铜", ["一、总览", "铜"]),
                ("paragraph", "乙", ["一、总览", "铜"]),
                ("heading", "二、风险", ["二、风险"]),
                ("paragraph", "丙", ["二、风险"]),
            ],
        )

    def test_br_split_and_wechat_heading_line(self) -> None:
        """无 p 的微信正文按 br 拆行，并识别短冒号标题行。"""
        source = (
            '<html><body><div id="js_content">'
            "已知品种：<br>沪铜<br>2026 年展望：<br>看多"
            "</div></body></html>"
        )
        result = extract_html(_article(source.encode("utf-8"), "html"))
        self.assertEqual(
            [(block.kind, block.text) for block in result.blocks],
            [
                ("heading", "已知品种："),
                ("paragraph", "沪铜"),
                ("heading", "2026 年展望："),
                ("paragraph", "看多"),
            ],
        )
        self.assertEqual(result.blocks[1].heading_path, ["已知品种："])
        self.assertEqual(result.blocks[3].heading_path, ["2026 年展望："])

    def test_table_grid_headers_and_row_label(self) -> None:
        """表格遵守 rowspan/colspan，并生成表头和行标签。"""
        source = (
            "<html><body><article><h2>价格</h2><table>"
            '<tr><th rowspan="2">品种</th><th colspan="2">价格</th>'
            '<th rowspan="2">观点</th></tr>'
            "<tr><th>收盘</th><th>涨跌</th></tr>"
            "<tr><th>cu2404</th><td>70000</td><td>+100</td><td>看多</td></tr>"
            "</table></article></body></html>"
        )
        result = extract_html(_article(source.encode("utf-8"), "html"))
        cells = {block.text: block for block in result.blocks if block.kind == "table_cell"}
        self.assertEqual(len(cells), 9)
        self.assertEqual((cells["看多"].table or {})["row"], 3)
        self.assertEqual((cells["看多"].table or {})["col"], 4)
        self.assertEqual((cells["看多"].table or {})["headers"], ["观点"])
        self.assertEqual((cells["70000"].table or {})["headers"], ["价格", "收盘"])
        self.assertEqual((cells["70000"].table or {})["row_label"], "cu2404")
        self.assertEqual((cells["70000"].table or {})["contract_text"], "cu2404")
        self.assertEqual(cells["看多"].heading_path, ["价格"])

    def test_manual_table_sample_is_one_based(self) -> None:
        """手工样例的表格行列从 1 开始，且表头为外层到内层。"""
        source = (
            "<html><body><article><table>"
            '<tr><th rowspan="2">品种</th><th colspan="2">走势</th></tr>'
            "<tr><th>日内</th><th>短期</th></tr>"
            "<tr><th>铜</th><td>震荡偏强</td><td>上涨</td></tr>"
            "</table></article></body></html>"
        )
        result = extract_html(_article(source.encode("utf-8"), "html"))
        cells = {block.text: block for block in result.blocks if block.kind == "table_cell"}
        table = cells["震荡偏强"].table or {}
        self.assertEqual(table["row"], 3)
        self.assertEqual(table["col"], 2)
        self.assertEqual(table["headers"], ["走势", "日内"])
        self.assertEqual(table["row_label"], "铜")
        self.assertIsNone(table["contract_text"])

    def test_attachment_refs(self) -> None:
        """扩展名匹配或带 download 的链接成为附件引用。"""
        source = (
            '<html><body><div id="js_content"><p>正文</p>'
            '<a href="files/a.PDF?download=1">附件一</a>'
            '<a href="/reports/b.html">附件二</a>'
            '<a href="/files/c.docx" download>下载文档</a>'
            '<a href="/files/d.txt">普通链接</a>'
            '<a href="https://example.com/e.png#preview">图片</a>'
            "</div></body></html>"
        )
        result = extract_html(_article(source.encode("utf-8"), "html"))
        self.assertEqual(
            [ref.href for ref in result.attachment_refs],
            [
                "files/a.PDF?download=1",
                "/reports/b.html",
                "/files/c.docx",
                "https://example.com/e.png#preview",
            ],
        )
        self.assertEqual(
            [ref.label for ref in result.attachment_refs],
            ["附件一", "附件二", "下载文档", "图片"],
        )
        self.assertTrue(
            all(
                ref.location.get("format") == "html"
                for ref in result.attachment_refs
            )
        )

    def test_body_missing(self) -> None:
        """只有导航时返回 body_missing 和空正文。"""
        source = "<html><body><nav>导航</nav><script>脚本</script></body></html>"
        result = extract_html(_article(source.encode("utf-8"), "html"))
        self.assertEqual(result.blocks, [])
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].code, "body_missing")
        self.assertEqual(result.issues[0].detail, "No readable report body")

    def test_encoding_error_result(self) -> None:
        """编码失败返回 encoding_error 且不抛异常。"""
        content = b"<html><body>" + bytes([0x81]) + b"</body></html>"
        result = extract_html(_article(content, "html"))
        self.assertEqual(result.blocks, [])
        self.assertEqual([issue.code for issue in result.issues], ["encoding_error"])


class PDFExtractTests(unittest.TestCase):
    """PDF 文本与错误分支测试。"""

    def test_layout_bbox_and_pages(self) -> None:
        """XHTML 行坐标、分页和页码映射正确。"""
        with patch("backend.parsers.pdf.subprocess.run") as run:
            run.return_value = _completed(_pdf_xhtml())
            result = extract_pdf(_article(b"%PDF-fake", "pdf"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], PDF_LAYOUT_COMMAND)
        self.assertEqual(run.call_args.kwargs["input"], b"%PDF-fake")
        self.assertEqual([block.text for block in result.blocks], [
            "Hello world沪铜2404",
            "第二行",
            "风险提示",
        ])
        first = result.blocks[0].location
        self.assertEqual(first["format"], "pdf")
        self.assertEqual(first["page"], 1)
        self.assertEqual(first["bbox"], [10.0, 20.0, 140.0, 35.0])
        self.assertEqual(first["page_size"], [600.0, 800.0])
        second = result.blocks[2].location
        self.assertEqual(second["page"], 2)
        self.assertEqual(second["bbox"], [5.0, 10.0, 60.0, 30.0])
        self.assertEqual(second["page_size"], [400.0, 500.0])

    def test_command_failure_falls_back_to_text(self) -> None:
        """命令失败后调用 -layout 回退并保留部分正文。"""
        failure = subprocess.CalledProcessError(1, ["pdftotext"], stderr=b"bad")
        fallback = _completed("第一页\n第二行\f第二页\n".encode("utf-8"))
        with patch("backend.parsers.pdf.subprocess.run") as run:
            run.side_effect = [failure, fallback]
            result = extract_pdf(_article(b"%PDF-fake", "pdf"))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0], PDF_TEXT_COMMAND)
        self.assertEqual(
            [issue.code for issue in result.issues],
            ["pdf_command_failed", "pdf_text_fallback"],
        )
        self.assertEqual([block.text for block in result.blocks], ["第一页", "第二行", "第二页"])
        self.assertIsNone(result.blocks[0].location["bbox"])
        self.assertIsNone(result.blocks[0].location["page_size"])
        self.assertEqual(result.blocks[2].location["page"], 2)

    def test_layout_failure_falls_back_to_text(self) -> None:
        """XHTML 损坏时记录 pdf_layout_failed 并回退。"""
        with patch("backend.parsers.pdf.subprocess.run") as run:
            run.side_effect = [
                _completed(b"not-xhtml"),
                _completed("回退正文".encode("utf-8")),
            ]
            result = extract_pdf(_article(b"%PDF-fake", "pdf"))
        self.assertEqual(
            [issue.code for issue in result.issues],
            ["pdf_layout_failed", "pdf_text_fallback"],
        )
        self.assertEqual([block.text for block in result.blocks], ["回退正文"])

    def test_tool_missing_falls_back_to_text(self) -> None:
        """缺少 pdftotext 时记录 pdf_tool_missing 并尝试回退。"""
        with patch("backend.parsers.pdf.subprocess.run") as run:
            run.side_effect = [
                FileNotFoundError("pdftotext"),
                _completed(b"fallback text"),
            ]
            result = extract_pdf(_article(b"%PDF-fake", "pdf"))
        self.assertEqual(
            [issue.code for issue in result.issues],
            ["pdf_tool_missing", "pdf_text_fallback"],
        )
        self.assertEqual([block.text for block in result.blocks], ["fallback text"])

    def test_timeout_falls_back_to_text(self) -> None:
        """超时记录 pdf_timeout 并尝试回退。"""
        timeout = subprocess.TimeoutExpired(["pdftotext"], 60)
        with patch("backend.parsers.pdf.subprocess.run") as run:
            run.side_effect = [timeout, _completed(b"timeout fallback")]
            result = extract_pdf(_article(b"%PDF-fake", "pdf"), timeout=5)
        self.assertEqual(
            [issue.code for issue in result.issues],
            ["pdf_timeout", "pdf_text_fallback"],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(result.blocks[0].text, "timeout fallback")

    def test_empty_text_layer(self) -> None:
        """没有文字行时返回 pdf_text_empty。"""
        empty = (
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b'<page width="10" height="10"></page></body></html>'
        )
        with patch("backend.parsers.pdf.subprocess.run") as run:
            run.return_value = _completed(empty)
            result = extract_pdf(_article(b"%PDF-fake", "pdf"))
        self.assertEqual(result.blocks, [])
        self.assertEqual([issue.code for issue in result.issues], ["pdf_text_empty"])
        self.assertEqual(run.call_count, 1)


class PNGExtractTests(unittest.TestCase):
    """PNG OCR 文本与错误分支测试。"""

    def test_tsv_lines_bbox_and_confidence(self) -> None:
        """TSV 按行聚合，并记录低置信度词。"""
        content = _png_bytes(640, 480)
        with patch("backend.parsers.png.subprocess.run") as run:
            run.return_value = _completed(_tesseract_tsv())
            result = extract_png(_article(content, "png"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], TESSERACT_COMMAND)
        self.assertEqual(run.call_args.kwargs["input"], content)
        self.assertEqual([block.text for block in result.blocks], ["沪铜", "看多", "风险"])
        first = result.blocks[0].location
        self.assertEqual(first["format"], "png")
        self.assertEqual(first["bbox"], [10, 20, 60, 40])
        self.assertEqual(first["image_size"], [640, 480])
        self.assertEqual(result.blocks[1].location["bbox"], [12, 50, 52, 70])
        self.assertEqual([issue.code for issue in result.issues], ["ocr_review"])
        self.assertIn("看多", result.issues[0].detail)
        self.assertEqual(result.issues[0].page, 1)

    def test_corrupt_png(self) -> None:
        """签名或 IHDR 无效时不调用 OCR。"""
        with patch("backend.parsers.png.subprocess.run") as run:
            result = extract_png(_article(b"not a png", "png"))
        run.assert_not_called()
        self.assertEqual([issue.code for issue in result.issues], ["png_corrupt"])
        self.assertEqual(result.blocks, [])

    def test_ocr_tool_missing(self) -> None:
        """缺少 tesseract 时返回 ocr_tool_missing。"""
        with patch("backend.parsers.png.subprocess.run") as run:
            run.side_effect = FileNotFoundError("tesseract")
            result = extract_png(_article(_png_bytes(), "png"))
        self.assertEqual([issue.code for issue in result.issues], ["ocr_tool_missing"])

    def test_ocr_timeout(self) -> None:
        """OCR 超时返回 ocr_timeout。"""
        timeout = subprocess.TimeoutExpired(["tesseract"], 60)
        with patch("backend.parsers.png.subprocess.run") as run:
            run.side_effect = timeout
            result = extract_png(_article(_png_bytes(), "png"), timeout=3)
        self.assertEqual([issue.code for issue in result.issues], ["ocr_timeout"])
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_ocr_command_failed(self) -> None:
        """OCR 返回非零状态时返回 ocr_failed。"""
        failure = subprocess.CalledProcessError(1, ["tesseract"])
        with patch("backend.parsers.png.subprocess.run") as run:
            run.side_effect = failure
            result = extract_png(_article(_png_bytes(), "png"))
        self.assertEqual([issue.code for issue in result.issues], ["ocr_failed"])


class WeChatVisibilityTest(unittest.TestCase):
    """微信正文容器初始隐藏时的正文提取。"""

    def test_visibility_hidden_content_is_kept(self) -> None:
        """``#js_content`` 带 ``visibility:hidden`` 时仍应抽取正文。"""
        html = (
            '<html><body><nav>首页</nav>'
            '<div id="js_content" style="visibility: hidden;">'
            "<h2>铁矿石</h2>"
            "<p>预计本周矿价维持震荡运行。</p>"
            "</div></body></html>"
        )
        result = extract_html(_article(html.encode("utf-8"), "html"))
        texts = [block.text for block in result.blocks]
        self.assertIn("铁矿石", texts)
        self.assertTrue(any("预计本周矿价" in text for text in texts))
        self.assertFalse(any("首页" in text for text in texts))
        self.assertEqual(result.issues, [])


class TableRowLabelFallbackTest(unittest.TestCase):
    """首列为品种时用最左单元格兜底行标签。"""

    def test_first_column_cell_becomes_row_label(self) -> None:
        """``<td>股指</td>`` 不是 ``th``，也应成为该行的 row_label。"""
        html = (
            "<html><body><div id=\"js_content\">"
            "<table>"
            "<tr><th>品种</th><th>分析要点</th><th>团队</th></tr>"
            "<tr><td>股指</td><td>四组期指偏强震荡。</td><td>宏观团队</td></tr>"
            "<tr><td>铜及国际铜</td><td>沪铜震荡为主。</td><td>金属团队</td></tr>"
            "</table>"
            "</div></body></html>"
        )
        result = extract_html(_article(html.encode("utf-8"), "html"))
        labels = {
            block.table["row_label"]
            for block in result.blocks
            if block.kind == "table_cell" and block.table is not None
        }
        self.assertIn("股指", labels)
        self.assertIn("铜及国际铜", labels)
        analysis = next(
            block
            for block in result.blocks
            if block.kind == "table_cell" and "四组期指" in block.text
        )
        self.assertEqual(analysis.table["row_label"], "股指")
        self.assertEqual(analysis.table["row"], 2)
        self.assertEqual(analysis.table["col"], 2)


if __name__ == "__main__":
    unittest.main()
