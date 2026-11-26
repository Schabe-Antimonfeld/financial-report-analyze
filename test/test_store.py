import pytest
from pathlib import Path

from backend.store import *


def test_detect_format():
    assert detect_format(b"") == ("unknown", "empty_file")
    assert detect_format(b"\x89PNG\r\n\x1a\n") == ("png", None)
    assert detect_format(b"%PDF-1.4") == ("pdf", None)
    assert detect_format(b"\xff\xfeH\0e\0l\0l\0o\0") == ("unknown", "unknown_signature")
    assert detect_format(b"\xef\xbb\xbf<html>") == ("html", None)
    assert detect_format(b"404 Not Found") == ("unknown", "source_error")


FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.parametrize(
    ("relative_path", "expected_format"),
    [
        ("data1/20240301/国信期货_210677_0.html", "html"),
        ("data1/20240305/华鑫期货_211752.PDF", "pdf"),
        ("data1/20240305/华鑫期货_211752_page1.png", "png"),
        ("data1/20240308/宏源期货_213132_0.html", "html"),
        ("data1/20240315/华泰期货_215145.PDF", "pdf"),
        ("data2/20240317/广金期货_216143.PDF", "pdf"),
        ("data2/20240325/东吴期货_218298.PDF", "pdf"),
        ("data2/20240327/宏源期货_218892_0.html", "html"),
        ("data2/20240329/宏源期货_219421_0.html", "html"),
    ],
)
def test_read_file(relative_path, expected_format):
    path = FIXTURES / relative_path
    expected_content = path.read_bytes()

    source = read_file(path, FIXTURES)

    assert source.relative_path == relative_path
    assert source.path_key == sha256(relative_path.encode("utf-8")).digest()
    assert source.content == expected_content
    assert source.content_hash == sha256(expected_content).digest()
    assert source.file_format == expected_format
    assert source.declared_extension == expected_format
    assert source.format_issue is None
    assert source.extension_mismatch is False


def test_read_file_mismatch(tmp_path):
    path = tmp_path / "report.PDF"
    path.write_bytes(b"<!DOCTYPE html><html><body>Report</body></html>")

    source = read_file(path, tmp_path)

    assert source.declared_extension == "pdf"
    assert source.file_format == "html"
    assert source.extension_mismatch is True


