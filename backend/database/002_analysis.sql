-- 阶段 04 · 分析、观点与证据表结构
-- 用法：mysql -u root -p futures_reports < backend/sql/002_analysis.sql
-- 依赖：先执行 001_raw.sql。字段与实施手册 04 的交接表一致。

USE futures_reports;

CREATE TABLE IF NOT EXISTS analysis_attempts (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    article_id BIGINT UNSIGNED NOT NULL,
    input_hash BINARY(32) NOT NULL,
    analyzer_version VARCHAR(64) NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    status VARCHAR(16) NOT NULL,
    error_code VARCHAR(64) NULL,
    result_count INT UNSIGNED NOT NULL DEFAULT 0,
    KEY ix_attempt_article (article_id, id),
    FOREIGN KEY (article_id) REFERENCES raw_articles(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS reports (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    raw_article_id BIGINT UNSIGNED NOT NULL,
    content_hash BINARY(32) NOT NULL,
    input_hash BINARY(32) NOT NULL,
    input_parts JSON NOT NULL,
    institution VARCHAR(128) NULL,
    title TEXT NULL,
    published_on DATE NULL,
    date_source VARCHAR(16) NULL,
    analyzer_version VARCHAR(64) NOT NULL,
    report_group_key BINARY(32) NOT NULL,
    metadata_review BOOLEAN NOT NULL DEFAULT FALSE,
    committed_attempt_id BIGINT UNSIGNED NULL,
    UNIQUE KEY uq_report_article (raw_article_id),
    KEY ix_report_group (report_group_key, published_on),
    KEY ix_report_day (published_on, institution),
    FOREIGN KEY (raw_article_id) REFERENCES raw_articles(id),
    FOREIGN KEY (content_hash) REFERENCES raw_contents(content_hash),
    FOREIGN KEY (committed_attempt_id) REFERENCES analysis_attempts(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS opinions (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    report_id BIGINT UNSIGNED NOT NULL,
    product_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    contract_key VARCHAR(24) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
    period_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    period_text VARCHAR(128) NULL,
    period_definition VARCHAR(128) COLLATE utf8mb4_bin NULL,
    direction ENUM('bullish', 'bearish', 'range') NULL,
    source ENUM('rule', 'llm') NULL,
    statement_type ENUM('unconditional', 'conditional', 'scenario', 'none') NOT NULL,
    review_state ENUM('recognized', 'unresolved', 'review') NOT NULL,
    reason_code VARCHAR(64) NULL,
    outlook_text TEXT NULL,
    conditions JSON NOT NULL,
    inference_reason TEXT NULL,
    sort_day DATE NOT NULL,
    FOREIGN KEY (report_id) REFERENCES reports(id),
    UNIQUE KEY uq_opinion_scope (report_id, product_id, contract_key, period_key),
    KEY ix_opinion_day (sort_day, id),
    KEY ix_opinion_product (product_id, period_key, sort_day, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS opinion_evidence (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    opinion_id BIGINT UNSIGNED NOT NULL,
    kind VARCHAR(24) NOT NULL,
    rule_id VARCHAR(64) NOT NULL DEFAULT '',
    grade CHAR(1) NOT NULL DEFAULT '',
    quote TEXT NOT NULL,
    block_id VARCHAR(64) NOT NULL,
    raw_article_id BIGINT UNSIGNED NOT NULL,
    content_hash BINARY(32) NOT NULL,
    location JSON NOT NULL,
    attachment_chain JSON NULL,
    contract VARCHAR(24) NULL,
    KEY ix_evidence_opinion (opinion_id),
    KEY ix_evidence_article (raw_article_id, content_hash),
    FOREIGN KEY (opinion_id) REFERENCES opinions(id) ON DELETE CASCADE,
    FOREIGN KEY (raw_article_id, content_hash)
        REFERENCES raw_article_versions(article_id, content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS model_attempts (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    attempt_id BIGINT UNSIGNED NOT NULL,
    scope_id VARCHAR(160) NOT NULL,
    fragment_hash BINARY(32) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    error_code VARCHAR(64) NULL,
    validated_result JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_model_cache
        (attempt_id, scope_id, fragment_hash, model_id, prompt_version),
    KEY ix_model_scope (scope_id, fragment_hash),
    FOREIGN KEY (attempt_id) REFERENCES analysis_attempts(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
