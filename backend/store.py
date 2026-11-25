from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re


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
    hash = sha256(content).digest()
    file_format, format_issue = detect_format(content[:8192])
    extension = path.suffix.lower().removeprefix(".")
    extension = "html" if extension == "htm" else extension
    source = SourceFile(
        relative_path=relative_path,
        path_key=sha256(relative_path.encode("utf-8")).digest(),
        content_hash=hash,
        content=content,
        file_format=file_format,
        format_issue=format_issue,
        declared_extension=extension,
        extension_mismatch=(file_format != "unknown" and file_format != extension),
    )
    return source


