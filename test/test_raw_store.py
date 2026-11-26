"""原文导入工具测试：格式识别、文件名拆解与相对路径。"""

from __future__ import annotations

import datetime
import hashlib
import tempfile
import unittest
from pathlib import Path

from backend.raw_store import (
    detect_format,
    extension_mismatch,
    parse_collected_on,
    parse_file_name,
    path_key_of,
    probe_file,
    read_file_bytes,
    relative_path_of,
)


class DetectFormatTest(unittest.TestCase):
    """按字节签名识别格式。"""

    def test_png_and_pdf_signatures(self) -> None:
        """PNG 八字节签名与 PDF 版本头。"""
        self.assertEqual(detect_format(b"\x89PNG\r\n\x1a\n" + b"0" * 32), ("png", None))
        self.assertEqual(detect_format(b"%PDF-1.7\n"), ("pdf", None))

    def test_html_with_bom_and_case(self) -> None:
        """BOM 与大小写不影响 HTML 识别。"""
        self.assertEqual(
            detect_format(b"\xef\xbb\xbf  <!DOCTYPE HTML><html>"), ("html", None)
        )
        self.assertEqual(detect_format(b"<html><body></body></html>"), ("html", None))

    def test_utf16_html_is_decoded_before_probe(self) -> None:
        """UTF-16 声明按 BOM 解码后再探测。"""
        content = b"\xff\xfe" + "<html><body>研报</body></html>".encode("utf-16-le")
        self.assertEqual(detect_format(content), ("html", None))

    def test_special_cases(self) -> None:
        """空文件、错误页与未知签名各有原因码。"""
        self.assertEqual(detect_format(b""), ("unknown", "empty_file"))
        self.assertEqual(detect_format(b"404 Not Found"), ("unknown", "source_error"))
        self.assertEqual(
            detect_format(b"\x00\x01\x02\x03"), ("unknown", "unknown_signature")
        )


class ExtensionMismatchTest(unittest.TestCase):
    """声明扩展名与实际格式的比较。"""

    def test_htm_and_html_are_equivalent(self) -> None:
        """``.htm`` 与 ``.html`` 视为同一格式。"""
        self.assertFalse(extension_mismatch("html", ".htm"))
        self.assertFalse(extension_mismatch("html", ".HTML"))
        self.assertTrue(extension_mismatch("html", ".pdf"))
        self.assertTrue(extension_mismatch("pdf", ".html"))

    def test_unknown_format_never_mismatches(self) -> None:
        """无法识别格式时不产生扩展名不匹配。"""
        self.assertFalse(extension_mismatch("unknown", ".pdf"))


class FileNameTest(unittest.TestCase):
    """文件名与采集日期拆解。"""

    def test_file_name_groups(self) -> None:
        """机构、采集编号、可选序号分别提取。"""
        self.assertEqual(
            parse_file_name("宝城期货_210703"), ("宝城期货", "210703", None)
        )
        self.assertEqual(
            parse_file_name("大越期货_210629_0"), ("大越期货", "210629", "0")
        )

    def test_unmatched_file_name_is_empty(self) -> None:
        """不符合约定时字段全为空。"""
        self.assertEqual(parse_file_name("报告"), (None, None, None))

    def test_collected_on(self) -> None:
        """目录名按 ``YYYYMMDD`` 解析。"""
        self.assertEqual(
            parse_collected_on("20240301"), datetime.date(2024, 3, 1)
        )
        self.assertIsNone(parse_collected_on("2024-03-01"))


class RelativePathTest(unittest.TestCase):
    """相对路径、路径键与首段探测。"""

    def setUp(self) -> None:
        """在仓库目录下创建临时资料根。"""
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.directory = self.root / "data1" / "20240301"
        self.directory.mkdir(parents=True)
        self.file = self.directory / "宝城期货_210703.PDF"
        self.file.write_bytes(b"%PDF-1.7\ncontent")

    def test_relative_path_is_posix(self) -> None:
        """相对路径统一用 ``/``，中文原样保留。"""
        relative = relative_path_of(self.file, self.root)
        self.assertEqual(relative, "data1/20240301/宝城期货_210703.PDF")
        self.assertEqual(len(path_key_of(relative)), 32)

    def test_outside_root_raises(self) -> None:
        """资料根之外的文件报 ``ValueError``。"""
        outside = Path(self.temporary.name) / "outside.pdf"
        outside.write_bytes(b"%PDF-1.7")
        with self.assertRaises(ValueError):
            relative_path_of(outside, self.root)

    def test_probe_and_hash(self) -> None:
        """探测只读首段；完整读取的指纹与 hashlib 一致。"""
        probe = probe_file(self.file, data_root=self.root)
        self.assertEqual(probe["format"], "pdf")
        self.assertEqual(probe["byte_size"], len(b"%PDF-1.7\ncontent"))
        content, digest, file_format, issue = read_file_bytes(self.file)
        self.assertEqual(content, b"%PDF-1.7\ncontent")
        self.assertEqual(digest, hashlib.sha256(content).hexdigest())
        self.assertEqual((file_format, issue), ("pdf", None))


if __name__ == "__main__":
    unittest.main()
