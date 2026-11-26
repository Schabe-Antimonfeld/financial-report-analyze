import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import mysql.connector
import pytest

from backend.store import (
    ArticleNotFound,
    detect_format,
    discover_files,
    find_article_id,
    ingest_file,
    load_article,
    read_file,
    store_source,
)


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


def test_discover_files(tmp_path):
    first = tmp_path / "data1" / "article.HTM"
    second = tmp_path / "data2" / "attachment.PNG"
    for path in (second, first, tmp_path / "data1" / "ignored.txt"):
        path.parent.mkdir(exist_ok=True)
        path.touch()

    assert discover_files(tmp_path) == [first, second]


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


@pytest.fixture
def article_db(db_connection, monkeypatch):
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_PORT", os.environ.get("TEST_DB_PORT", "3306"))
    monkeypatch.setenv("DB_NAME", db_connection.database)
    monkeypatch.setenv("DB_USER", os.environ["TEST_DB_USER"])
    monkeypatch.setenv("DB_PASSWORD", os.environ.get("TEST_DB_PASSWORD", ""))
    return db_connection


@pytest.mark.parametrize(("relative_path", "expected_format"), SAMPLES)
def test_ingest_file_round_trip(relative_path, expected_format, article_db):
    path = FIXTURES / relative_path

    first = ingest_file(path, data_root=FIXTURES)
    second = ingest_file(path, data_root=FIXTURES)
    article = load_article(first.article_id)

    assert first.status == "inserted"
    assert second.status == "skipped"
    assert first.article_id == second.article_id == find_article_id(relative_path)
    assert article.relative_path == relative_path
    assert article.format == first.format == expected_format
    assert article.content == path.read_bytes()
    assert article.content_hash == first.content_hash == sha256(article.content).hexdigest()
    assert find_article_id(relative_path + ".missing") is None


def test_load_article_reads_large_blob(tmp_path, article_db):
    path = tmp_path / "report.PDF"
    content = b"%PDF-1.7\n" + bytes(range(256)) * 65_536
    path.write_bytes(content)

    result = ingest_file(path, data_root=tmp_path)

    assert load_article(result.article_id).content == content
    assert load_article(result.article_id, result.content_hash).content == content


def test_ingest_file_preserves_duplicate_contents_and_old_version(tmp_path, article_db):
    folder = tmp_path / "20240301"
    folder.mkdir()
    path = folder / "机构_123_0.PDF"
    old_content = b"<html>Older report</html>"
    path.write_bytes(old_content)
    first = ingest_file(path, data_root=tmp_path)
    duplicate = folder / "另一机构_124.PDF"
    duplicate.write_bytes(old_content)
    copy = ingest_file(duplicate, data_root=tmp_path)

    assert copy.article_id != first.article_id
    assert copy.content_hash == first.content_hash
    with article_db.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM raw_contents")
        assert cursor.fetchone() == (1,)
    article_db.rollback()

    new_content = b"%PDF-1.7\nNew report"
    path.write_bytes(new_content)
    updated = ingest_file(path, data_root=tmp_path)
    path.rename(folder / "offline.PDF")

    assert updated.status == "updated"
    assert updated.article_id == first.article_id
    assert updated.content_hash != first.content_hash
    current = load_article(first.article_id)
    assert current.content == new_content
    assert current.collected_on.isoformat() == "2024-03-01"
    assert current.institution_hint == "机构"
    assert current.extension_mismatch is False
    historical = load_article(first.article_id, first.content_hash)
    assert historical.content == old_content
    assert historical.format == "html"
    assert historical.extension_mismatch is True
    with pytest.raises(ArticleNotFound):
        load_article(copy.article_id, updated.content_hash)
    with pytest.raises(ValueError):
        load_article(copy.article_id, "00")


@pytest.mark.parametrize(
    ("content", "issue"), [(b"", "empty_file"), (b"unsupported", "unknown_signature")]
)
def test_ingest_file_preserves_format_issue(tmp_path, article_db, content, issue):
    path = tmp_path / "report.PDF"
    path.write_bytes(content)

    result = ingest_file(path, data_root=tmp_path)
    article = load_article(result.article_id)

    assert article.content == content
    assert article.format == "unknown"
    assert article.format_issue == issue
    assert article.extension_mismatch is False


def test_store_source_rolls_back_after_content_insert(tmp_path, article_db, monkeypatch):
    path = tmp_path / "report.html"
    path.write_bytes(b"<html>Report</html>")
    source = read_file(path, tmp_path)
    original_cursor = article_db.cursor

    with monkeypatch.context() as patch:
        def failing_cursor(**options):
            cursor = original_cursor(**options)
            execute = cursor.execute

            def fail_after_content(statement, *params):
                if statement.lstrip().startswith("INSERT INTO raw_articles"):
                    raise RuntimeError("Injected failure after storing contents")
                return execute(statement, *params)

            patch.setattr(cursor, "execute", fail_after_content)
            return cursor

        patch.setattr(article_db, "cursor", failing_cursor)
        with pytest.raises(RuntimeError, match="Injected failure"):
            store_source(article_db, source)

    with article_db.cursor() as cursor:
        cursor.execute(
            """SELECT (SELECT COUNT(*) FROM raw_contents),
                      (SELECT COUNT(*) FROM raw_articles),
                      (SELECT COUNT(*) FROM raw_article_versions)"""
        )
        assert cursor.fetchone() == (0, 0, 0)

    assert ingest_file(path, data_root=tmp_path).status == "inserted"