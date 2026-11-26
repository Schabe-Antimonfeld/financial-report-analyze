import json
import os
import re
from argparse import ArgumentParser
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath

from mysql.connector import Error as MySQLError
from mysql.connector import MySQLConnection, connect
from mysql.connector.errorcode import ER_DUP_ENTRY
from mysql.connector.errors import IntegrityError


DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
EXTENSIONS = {".pdf", ".html", ".htm", ".png"}


@dataclass
class SourceFile:
    relative_path: str
    path_key: bytes
    content_hash: bytes
    content: bytes
    file_format: str
    format_issue: str | None
    declared_extension: str
    extension_mismatch: bool


@dataclass
class IngestResult:
    article_id: int
    relative_path: str
    content_hash: str
    format: str
    status: str
    error_code: str | None = None


@dataclass
class RawArticle:
    article_id: int
    relative_path: str
    content_hash: str
    format: str
    content: bytes
    collected_on: date | None
    institution_hint: str | None
    format_issue: str | None
    extension_mismatch: bool


class ArticleNotFound(LookupError):
    """The requested article or article version is not in the database."""


def detect_format(head: bytes) -> tuple[str, str | None]:
    """
    Detect the format of a file based on its head bytes.

    Args:
        head (bytes): The first few bytes of the file to analyze.

    Returns:
        tuple[str, str | None]: A tuple containing the detected format and an optional error message.
    """
    if not head:
        return "unknown", "empty_file"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", None
    if re.search(rb"%PDF-\d\.\d", head[:1024]):
        return "pdf", None

    prefix = head[:8192]

    for bom, encoding in (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if prefix.startswith(bom):
            prefix = prefix.decode(encoding, errors="ignore").encode("utf-8")
            break

    prefix = prefix.removeprefix(b"\xef\xbb\xbf").lstrip()

    if re.search(
        rb"<!doctype\s+html\b|<(?:html|head|body|main|article|div|p)(?:\s|>)",
        prefix,
        re.I,
    ):
        return "html", None
    if re.match(rb"(?:404\b|not found\b)", prefix, re.I):
        return "unknown", "source_error"
    return "unknown", "unknown_signature"


def _extension_mismatch(extension: str, file_format: str) -> bool:
    expected = "html" if extension == "htm" else extension
    return file_format != "unknown" and file_format != expected


def read_file(path: Path, data_root: Path) -> SourceFile:
    """
    Read a file from the given path and return a SourceFile object containing its metadata.

    Args:
        path (Path): The path to the file to read.
        data_root (Path): The root directory to which the file path is relative.

    Returns:
        SourceFile: An object containing the file's relative path, content hash, content, detected format, and other metadata.
    """
    path = path.resolve()
    relative_path = path.relative_to(data_root.resolve()).as_posix()
    content = path.read_bytes()
    digest = sha256(content)
    file_format, format_issue = detect_format(content[:8192])
    extension = path.suffix.lower().removeprefix(".")
    return SourceFile(
        relative_path=relative_path,
        path_key=sha256(relative_path.encode("utf-8")).digest(),
        content_hash=digest.digest(),
        content=content,
        file_format=file_format,
        format_issue=format_issue,
        declared_extension=extension,
        extension_mismatch=_extension_mismatch(extension, file_format),
    )


def discover_files(data_root: Path = DATA_ROOT) -> list[Path]:
    """
    Find supported files under data1 and data2 in normalized path order.

    Args:
        data_root (Path): The root directory to search for files.·

    Returns:
        list[Path]: A sorted list of Path objects representing the discovered files.
    """
    root = data_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)

    files = set()
    for batch in ("data1", "data2"):
        for path in (root / batch).rglob("*"):
            if path.suffix.lower() in EXTENSIONS and path.is_file():
                resolved = path.resolve()
                if resolved.is_relative_to(root):
                    files.add(resolved)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _path_metadata(relative_path: str) -> tuple[date | None, str | None, str | None, str | None]:
    path = PurePosixPath(relative_path)
    collected_on = None
    if re.fullmatch(r"[0-9]{8}", path.parent.name):
        try:
            collected_on = datetime.strptime(path.parent.name, "%Y%m%d").date()
        except ValueError:
            pass

    name = re.fullmatch(r"(.+?)_([0-9]+)(?:_([0-9]+))?", path.stem)
    if name is None:
        return collected_on, None, None, None

    institution, code, part = name.groups()
    return (
        collected_on,
        institution if len(institution) <= 128 else None,
        code if len(code) <= 64 else None,
        part if part is not None and len(part) <= 32 else None,
    )


def store_source(connection: MySQLConnection, source: SourceFile) -> tuple[int, str]:
    '''
    Store the given SourceFile object in the database.
    
    Args:
        connection (MySQLConnection): The MySQL database connection.
        source (SourceFile): The SourceFile object to store.

    Returns:
        tuple[int, str]: A tuple containing the article ID and the status of the operation ("inserted", "skipped", or "updated").
    '''
    metadata = (
        *_path_metadata(source.relative_path),
        source.declared_extension,
        source.extension_mismatch,
    )
    connection.start_transaction()
    try:
        with connection.cursor(prepared=True) as cursor:
            cursor.execute(
                "SELECT 1 FROM raw_contents WHERE content_hash = %s",
                (source.content_hash,),
            )
            if not cursor.fetchall():
                cursor.execute(
                    """
                    INSERT INTO raw_contents
                        (content_hash, content, byte_size, file_format, format_issue)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE content_hash = raw_contents.content_hash
                    """,
                    (
                        source.content_hash,
                        source.content,
                        len(source.content),
                        source.file_format,
                        source.format_issue,
                    ),
                )

            try:
                cursor.execute(
                    """
                    INSERT INTO raw_articles
                        (relative_path, path_key, content_hash,
                         collected_on, institution_hint, collect_code, collect_part,
                         declared_extension, extension_mismatch)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        source.relative_path,
                        source.path_key,
                        source.content_hash,
                        *metadata,
                    ),
                )
                article_id = cursor.lastrowid
                status = "inserted"
            except IntegrityError as error:
                if error.errno != ER_DUP_ENTRY:
                    raise

                cursor.execute(
                    """
                    SELECT id, relative_path, content_hash,
                           collected_on, institution_hint, collect_code, collect_part,
                           declared_extension, extension_mismatch
                    FROM raw_articles
                    WHERE path_key = %s
                    FOR UPDATE
                    """,
                    (source.path_key,),
                )
                [(article_id, relative_path, current_hash, *current_metadata)] = cursor.fetchall()

                if relative_path != source.relative_path:
                    raise ValueError("Path key matches a different relative path")

                changed = current_hash != source.content_hash
                status = "updated" if changed else "skipped"
                if changed or tuple(current_metadata) != metadata:
                    cursor.execute(
                        """
                        UPDATE raw_articles
                        SET content_hash = %s, collected_on = %s, institution_hint = %s,
                            collect_code = %s, collect_part = %s,
                            declared_extension = %s, extension_mismatch = %s
                        WHERE id = %s
                        """,
                        (source.content_hash, *metadata, article_id),
                    )

            cursor.execute(
                """
                INSERT INTO raw_article_versions (article_id, content_hash)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE seen_at = raw_article_versions.seen_at
                """,
                (article_id, source.content_hash),
            )

        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return article_id, status


def _connect() -> MySQLConnection:
    return connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        database=os.environ.get("DB_NAME", "futures_reports"),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        charset="utf8mb4",
    )


def _ingest_source(connection: MySQLConnection, source: SourceFile) -> IngestResult:
    article_id, status = store_source(connection, source)
    return IngestResult(
        article_id, source.relative_path, source.content_hash.hex(), source.file_format, status
    )


def ingest_file(path: str | Path, *, data_root: Path = DATA_ROOT) -> IngestResult:
    """
    Read a file under data_root and commit its content and version.

    Args:
        path (str | Path): The path to the file to ingest.
        data_root (Path): The root directory to which the file path is relative.

    Returns:
        IngestResult: An object containing the article ID, relative path, content hash, format, and status of the ingestion operation.
    """
    source = read_file(Path(path), data_root)
    with _connect() as connection:
        return _ingest_source(connection, source)


def load_article(article_id: int, content_hash: str | None = None) -> RawArticle:
    """
    Return the current article or a version belonging to that article.
    
    Args:
        article_id (int): The ID of the article to load.
        content_hash (str | None): Optional SHA-256 hex string of the content hash to load a specific version.

    Returns:
        RawArticle: An object containing the article's metadata and content.
    """
    query = """
        SELECT a.id, a.relative_path, c.content_hash, c.file_format, c.content,
               a.collected_on, a.institution_hint, c.format_issue, a.declared_extension
        FROM raw_articles AS a
    """
    if content_hash is None:
        query += """
            JOIN raw_contents AS c ON c.content_hash = a.content_hash
            WHERE a.id = %s
        """
        params = (article_id,)
    else:
        digest = bytes.fromhex(content_hash)
        if len(content_hash) != 64 or len(digest) != 32:
            raise ValueError("Expected a 64-character SHA-256 hex string")
        query += """
            JOIN raw_article_versions AS v ON v.article_id = a.id
            JOIN raw_contents AS c ON c.content_hash = v.content_hash
            WHERE a.id = %s AND v.content_hash = UNHEX(%s)
        """
        params = (article_id, digest.hex())

    # The pure-Python driver reassembles multi-packet BLOBs on the text result path.
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    if not rows:
        raise ArticleNotFound(f"No article or version found for article ID {article_id}")

    [(article_id, path, digest, file_format, content, collected_on, institution, issue, extension)] = rows
    return RawArticle(
        article_id=article_id,
        relative_path=path,
        content_hash=digest.hex(),
        format=file_format,
        content=bytes(content),
        collected_on=collected_on,
        institution_hint=institution,
        format_issue=issue,
        extension_mismatch=_extension_mismatch(extension, file_format),
    )


def find_article_id(relative_path: str) -> int | None:
    """
    Find the article ID for an exact normalized path under the data root.
    
    Args:
        relative_path (str): The normalized relative path of the article.

    Returns:
        int | None: The article ID if found, otherwise None.
    """
    path_key = sha256(relative_path.encode("utf-8")).digest()
    with _connect() as connection, connection.cursor(prepared=True) as cursor:
        cursor.execute(
            "SELECT id, relative_path FROM raw_articles WHERE path_key = %s", (path_key,)
        )
        rows = cursor.fetchall()
    if rows and rows[0][1] == relative_path:
        return rows[0][0]
    return None


def main() -> int:
    """Discover or import a data root, emitting one JSON record per file."""
    parser = ArgumentParser(description="Import raw financial reports into MySQL")
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and ("DB_USER" not in os.environ or "DB_PASSWORD" not in os.environ):
        parser.error("Set DB_USER and DB_PASSWORD before importing")

    root = args.data_root.resolve()
    if not root.is_dir():
        parser.error(f"Data root is not a directory: {root}")

    had_errors = False
    connection = None
    try:
        for path in discover_files(root):
            relative_path = path.relative_to(root).as_posix()
            if args.dry_run:
                result = {"relative_path": relative_path, "status": "discovered"}
            else:
                try:
                    source = read_file(path, root)
                    if connection is None:
                        connection = _connect()
                    result = asdict(_ingest_source(connection, source))
                except (MySQLError, OSError, ValueError) as error:
                    had_errors = True
                    error_code = type(error).__name__
                    if isinstance(error, MySQLError):
                        error_code = f"mysql_{error.errno}"
                        if connection is not None:
                            with suppress(MySQLError, OSError):
                                connection.close()
                            connection = None
                    result = {
                        "relative_path": relative_path, "status": "error", "error_code": error_code
                    }
            print(json.dumps(result))
    finally:
        if connection is not None:
            connection.close()
    return int(had_errors)


if __name__ == "__main__":
    raise SystemExit(main())
