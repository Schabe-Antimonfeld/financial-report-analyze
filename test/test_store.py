from backend.store import *


def test_detect_format():
    assert detect_format(b"") == ("unknown", "empty_file")
    assert detect_format(b"\x89PNG\r\n\x1a\n") == ("png", None)
    assert detect_format(b"%PDF-1.4") == ("pdf", None)
    assert detect_format(b"\xff\xfeH\0e\0l\0l\0o\0") == ("unknown", "unknown_signature")
    assert detect_format(b"\xef\xbb\xbf<html>") == ("html", None)
    assert detect_format(b"404 Not Found") == ("unknown", "source_error")
