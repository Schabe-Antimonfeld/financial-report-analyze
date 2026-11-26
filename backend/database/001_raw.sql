CREATE DATABASE IF NOT EXISTS futures_reports CHARACTER SET utf8mb4;
USE futures_reports;

CREATE TABLE IF NOT EXISTS raw_contents (
    content_hash BINARY(32) PRIMARY KEY,
    content LONGBLOB NOT NULL,
    byte_size BIGINT UNSIGNED NOT NULL,
    file_format VARCHAR(12) NOT NULL,
    format_issue VARCHAR(32) NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS raw_articles (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    path_key BINARY(32) NOT NULL UNIQUE,
    content_hash BINARY(32) NOT NULL,
    collected_on DATE NULL,
    institution_hint VARCHAR(128) NULL,
    collect_code VARCHAR(64) NULL,
    collect_part VARCHAR(32) NULL,
    declared_extension VARCHAR(12) NOT NULL,
    extension_mismatch BOOLEAN NOT NULL DEFAULT FALSE,
    FOREIGN KEY (content_hash) REFERENCES raw_contents(content_hash)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS raw_article_versions (
    article_id BIGINT UNSIGNED NOT NULL,
    content_hash BINARY(32) NOT NULL,
    seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (article_id, content_hash),
    FOREIGN KEY (article_id) REFERENCES raw_articles(id),
    FOREIGN KEY (content_hash) REFERENCES raw_contents(content_hash)
) ENGINE=InnoDB;