import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import mysql.connector
import pytest

from backend.store import detect_format, read_file, store_source


def test_detect_format():
    assert detect_format(b"") == ("unknown", "empty_file")
    assert detect_format(b"\x89PNG\r\n\x1a\n") == ("png", None)
    assert detect_format(b"%PDF-1.4") == ("pdf", None)
    assert detect_format(b"\xff\xfeH\0e\0l\0l\0o\0") == ("unknown", "unknown_signature")
    assert detect_format(b"\xef\xbb\xbf<html>") == ("html", None)
    assert detect_format(b"404 Not Found") == ("unknown", "source_error")


FIXTURES = Path(__file__).resolve().parent / "fixtures"
MIGRATION = FIXTURES.parents[1] / "backend" / "database" / "001_raw.sql"
SAMPLES = [
    ("data1/20240301/国信期货_210677_0.html", "html"),
    ("data1/20240305/华鑫期货_211752.PDF", "pdf"),
    ("data1/20240305/华鑫期货_211752_page1.png", "png"),
    ("data1/20240308/宏源期货_213132_0.html", "html"),
    ("data1/20240315/华泰期货_215145.PDF", "pdf"),
    ("data2/20240317/广金期货_216143.PDF", "pdf"),
    ("data2/20240325/东吴期货_218298.PDF", "pdf"),
    ("data2/20240327/宏源期货_218892_0.html", "html"),
    ("data2/20240329/宏源期货_219421_0.html", "html"),
]


@pytest.mark.parametrize(("relative_path", "expected_format"), SAMPLES)
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


# Remeber to set the environment variable and create a MySQL user with the appropriate permissions before running this test.
@pytest.fixture
def db_connection():
    user = os.environ.get("TEST_DB_USER")
    if not user:
        pytest.skip("Set TEST_DB_USER to run the MySQL integration tests")

    database = f"futures_reports_test_{uuid4().hex}"
    migration = MIGRATION.read_text(encoding="utf-8-sig")

    with mysql.connector.connect(
        host="127.0.0.1",
        port=int(os.environ.get("TEST_DB_PORT", "3306")),
        user=user,
        password=os.environ.get("TEST_DB_PASSWORD", ""),
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")

        try:
            connection.database = database
            with connection.cursor() as cursor:
                for statement in migration.split(";"):
                    statement = statement.strip()
                    if statement.startswith("CREATE TABLE "):
                        cursor.execute(statement)

            yield connection
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP DATABASE `{database}`")


@pytest.mark.parametrize("relative_path", [path for path, _ in SAMPLES])
def test_store_source(relative_path, db_connection):
    source = read_file(FIXTURES / relative_path, FIXTURES)

    article_id, status = store_source(db_connection, source)

    assert status == "inserted"
    assert article_id > 0
    assert store_source(db_connection, source) == (article_id, "skipped")
