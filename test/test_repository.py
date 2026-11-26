"""原文适配层测试：路径键、文件发现与格式探测。

依赖 ``backend/store.py`` 的用例在模块缺失时自动跳过，保证只改阶段 06 时也能
单独跑测试。
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from backend.repository import (
    _declared_extension,
    _extension_mismatch,
    iter_discovered_files,
    path_key_of,
    probe_file,
    relative_path_of,
)

STORE_AVAILABLE = importlib.util.find_spec("backend.store") is not None
if STORE_AVAILABLE:  # pragma: no branch - 仓库里正常存在
    from backend.store import detect_format
else:  # pragma: no cover - 仅用于单独验证阶段 06
    detect_format = None


class RelativePathTest(unittest.TestCase):
    """相对路径与路径键。"""

    def setUp(self) -> None:
        """在仓库目录下创建临时资料根。"""
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.directory = self.root / "data1" / "20240301"
        self.directory.mkdir(parents=True)

    def test_relative_path_is_posix(self) -> None:
        """相对路径统一用 ``/``，中文原样保留。"""
        target = self.directory / "宝城期货_210703.PDF"
        target.write_bytes(b"%PDF-1.7")
        relative = relative_path_of(target, self.root)
        self.assertEqual(relative, "data1/20240301/宝城期货_210703.PDF")
        self.assertEqual(len(path_key_of(relative)), 32)

    def test_outside_root_raises(self) -> None:
        """资料根之外的文件报 ``ValueError``。"""
        outside = Path(self.temporary.name) / "outside.pdf"
        outside.write_bytes(b"%PDF-1.7")
        with self.assertRaises(ValueError):
            relative_path_of(outside, self.root)

    def test_declared_extension(self) -> None:
        """``.htm`` 归一为 ``html``，其余保持小写。"""
        self.assertEqual(_declared_extension(Path("a.HTM")), "html")
        self.assertEqual(_declared_extension(Path("a.PDF")), "pdf")


class ExtensionMismatchTest(unittest.TestCase):
    """声明扩展名与实际格式的比较。"""

    def test_htm_and_html_are_equivalent(self) -> None:
        """``.htm`` 与 ``.html`` 视为同一格式。"""
        self.assertFalse(_extension_mismatch("html", "html"))
        self.assertTrue(_extension_mismatch("html", "pdf"))
        self.assertTrue(_extension_mismatch("pdf", "html"))

    def test_unknown_format_never_mismatches(self) -> None:
        """无法识别格式时不产生扩展名不匹配。"""
        self.assertFalse(_extension_mismatch("unknown", "pdf"))


@unittest.skipUnless(STORE_AVAILABLE, "需要阶段 01 的 backend/store.py")
class DetectFormatTest(unittest.TestCase):
    """复用 store.py 的签名识别。"""

    def test_png_and_pdf_signatures(self) -> None:
        """PNG 八字节签名与 PDF 版本头。"""
        self.assertEqual(
            detect_format(b"\x89PNG\r\n\x1a\n" + b"0" * 32), ("png", None)
        )
        self.assertEqual(detect_format(b"%PDF-1.7\n"), ("pdf", None))

    def test_html_with_bom_and_case(self) -> None:
        """BOM 与大小写不影响 HTML 识别。"""
        self.assertEqual(
            detect_format(b"\xef\xbb\xbf  <!DOCTYPE HTML><html>"), ("html", None)
        )
        self.assertEqual(detect_format(b"<html><body></body></html>"), ("html", None))

    def test_special_cases(self) -> None:
        """空文件、错误页与未知签名各有原因码。"""
        self.assertEqual(detect_format(b""), ("unknown", "empty_file"))
        self.assertEqual(detect_format(b"404 Not Found"), ("unknown", "source_error"))
        self.assertEqual(
            detect_format(b"\x00\x01\x02\x03"), ("unknown", "unknown_signature")
        )


@unittest.skipUnless(STORE_AVAILABLE, "需要阶段 01 的 backend/store.py")
class FileDiscoveryTest(unittest.TestCase):
    """文件发现与首段探测。"""

    def setUp(self) -> None:
        """准备 data1/data2 两个批次目录。"""
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        first = self.root / "data1" / "20240301"
        second = self.root / "data2" / "20240302"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        self.pdf = first / "宝城期货_210703.PDF"
        self.pdf.write_bytes(b"%PDF-1.7\ncontent")
        self.html = second / "大越期货_210629_0.html"
        self.html.write_text(
            "<html><body>研报</body></html>", encoding="utf-8"
        )
        self.other = first / "说明.txt"
        self.other.write_text("忽略", encoding="utf-8")

    def test_discovery_is_sorted_and_skips_other_suffixes(self) -> None:
        """只返回支持的后缀，按规范相对路径排序。"""
        found = list(iter_discovered_files(self.root))
        relatives = [relative for _path, relative in found]
        self.assertEqual(
            relatives,
            [
                "data1/20240301/宝城期货_210703.PDF",
                "data2/20240302/大越期货_210629_0.html",
            ],
        )

    def test_probe_file_reads_only_head(self) -> None:
        """探测返回格式、字节数，不写库。"""
        probe = probe_file(self.pdf, data_root=self.root)
        self.assertEqual(probe["format"], "pdf")
        self.assertEqual(probe["byte_size"], len(b"%PDF-1.7\ncontent"))
        self.assertFalse(probe["extension_mismatch"])
        self.assertEqual(
            probe["relative_path"], "data1/20240301/宝城期货_210703.PDF"
        )

    def test_probe_detects_extension_mismatch(self) -> None:
        """扩展名为 PDF、内容为 HTML 时标不匹配。"""
        fake = self.root / "data1" / "20240301" / "恒泰期货_210639.PDF"
        fake.write_text("<html><body>研报</body></html>", encoding="utf-8")
        probe = probe_file(fake, data_root=self.root)
        self.assertEqual(probe["format"], "html")
        self.assertTrue(probe["extension_mismatch"])


if __name__ == "__main__":
    unittest.main()
