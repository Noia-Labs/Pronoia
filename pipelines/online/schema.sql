PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cohorts (
    cohort_date TEXT PRIMARY KEY,
    collected_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    frozen INTEGER NOT NULL CHECK (frozen IN (0, 1)),
    raw_count INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    validated_count INTEGER NOT NULL,
    quarantine_count INTEGER NOT NULL,
    prediction_eligible_count INTEGER NOT NULL,
    manifest_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_records (
    record_id TEXT PRIMARY KEY,
    cohort_date TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    market TEXT,
    symbol TEXT,
    issuer_name TEXT,
    announcement_date TEXT,
    announcement_type TEXT,
    title TEXT,
    source_url TEXT,
    collected_at TEXT,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (cohort_date) REFERENCES cohorts(cohort_date),
    UNIQUE (cohort_date, source, source_key)
);

CREATE INDEX IF NOT EXISTS idx_source_records_cohort
    ON source_records(cohort_date);
CREATE INDEX IF NOT EXISTS idx_source_records_symbol_date
    ON source_records(market, symbol, announcement_date);

-- Broad catalog: every collected source record is classified, including records
-- that are not suitable for standalone direction prediction.
CREATE TABLE IF NOT EXISTS classified_event_catalog (
    catalog_event_id TEXT PRIMARY KEY,
    cohort_date TEXT NOT NULL,
    source_record_id TEXT NOT NULL UNIQUE,
    market TEXT,
    symbol TEXT,
    native_event_type TEXT,
    normalized_event_type TEXT NOT NULL,
    catalog_role TEXT NOT NULL
        CHECK (catalog_role IN ('prediction_candidate', 'review', 'evidence_only')),
    information_tier TEXT NOT NULL,
    packet_status TEXT NOT NULL,
    required_fields_json TEXT NOT NULL,
    decision_checks_json TEXT NOT NULL,
    preferred_horizons_json TEXT NOT NULL,
    classification_json TEXT NOT NULL,
    FOREIGN KEY (cohort_date) REFERENCES cohorts(cohort_date),
    FOREIGN KEY (source_record_id) REFERENCES source_records(record_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_classified_catalog_queue
    ON classified_event_catalog(catalog_role, normalized_event_type, cohort_date);
CREATE INDEX IF NOT EXISTS idx_classified_catalog_symbol
    ON classified_event_catalog(market, symbol, cohort_date);

CREATE TABLE IF NOT EXISTS metadata_event_screening (
    catalog_event_id TEXT NOT NULL,
    screening_version TEXT NOT NULL,
    score_threshold INTEGER NOT NULL,
    total_score INTEGER NOT NULL CHECK (total_score BETWEEN 0 AND 100),
    hard_gate_pass INTEGER NOT NULL CHECK (hard_gate_pass IN (0, 1)),
    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
    score_breakdown_json TEXT NOT NULL,
    hard_gate_failures_json TEXT NOT NULL,
    screening_json TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    PRIMARY KEY (catalog_event_id, screening_version),
    FOREIGN KEY (catalog_event_id) REFERENCES classified_event_catalog(catalog_event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_metadata_event_screening_queue
    ON metadata_event_screening(screening_version, selected, total_score);

CREATE TABLE IF NOT EXISTS metadata_event_packets (
    event_id TEXT PRIMARY KEY,
    catalog_event_id TEXT NOT NULL UNIQUE,
    packet_version TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_time TEXT NOT NULL,
    event_type_l2 TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (catalog_event_id) REFERENCES classified_event_catalog(catalog_event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_metadata_event_packets_run
    ON metadata_event_packets(event_type_l2, market, event_time);

CREATE TABLE IF NOT EXISTS event_candidates (
    candidate_id TEXT PRIMARY KEY,
    cohort_date TEXT NOT NULL,
    source_record_id TEXT,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_type_l2 TEXT NOT NULL,
    title TEXT NOT NULL,
    quality_score INTEGER NOT NULL,
    document_fetch_ok INTEGER NOT NULL CHECK (document_fetch_ok IN (0, 1)),
    document_fetch_error TEXT,
    candidate_json TEXT NOT NULL,
    FOREIGN KEY (cohort_date) REFERENCES cohorts(cohort_date),
    FOREIGN KEY (source_record_id) REFERENCES source_records(record_id)
);

CREATE INDEX IF NOT EXISTS idx_event_candidates_cohort
    ON event_candidates(cohort_date);
CREATE INDEX IF NOT EXISTS idx_event_candidates_type
    ON event_candidates(market, event_type_l2, quality_score);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    cohort_date TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    issuer_name TEXT,
    event_time TEXT NOT NULL,
    published_at TEXT,
    effective_session TEXT,
    prediction_cutoff_at TEXT,
    event_type_l2 TEXT NOT NULL,
    title TEXT NOT NULL,
    event_text TEXT NOT NULL,
    source_url TEXT NOT NULL,
    benchmark TEXT,
    quality_score INTEGER NOT NULL,
    quality_pass INTEGER NOT NULL CHECK (quality_pass IN (0, 1)),
    evaluation_status TEXT NOT NULL
        CHECK (evaluation_status IN ('strict', 'review', 'exclude', 'duplicate')),
    evaluation_selection_reason TEXT,
    prediction_eligible INTEGER NOT NULL CHECK (prediction_eligible IN (0, 1)),
    source_document_hash TEXT,
    source_document_count INTEGER NOT NULL DEFAULT 1,
    event_json TEXT NOT NULL,
    FOREIGN KEY (cohort_date) REFERENCES cohorts(cohort_date)
);

CREATE INDEX IF NOT EXISTS idx_events_queue
    ON events(evaluation_status, prediction_eligible, cohort_date);
CREATE INDEX IF NOT EXISTS idx_events_symbol_time
    ON events(market, symbol, event_time);
CREATE INDEX IF NOT EXISTS idx_events_type
    ON events(event_type_l2, market);

CREATE TABLE IF NOT EXISTS prediction_event_eligibility (
    event_id TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    dataset_scope TEXT NOT NULL,
    hard_gate_pass INTEGER NOT NULL CHECK (hard_gate_pass IN (0, 1)),
    total_score INTEGER NOT NULL CHECK (total_score BETWEEN 0 AND 100),
    score_threshold INTEGER NOT NULL,
    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
    score_breakdown_json TEXT NOT NULL,
    hard_gate_failures_json TEXT NOT NULL,
    assessment_json TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    PRIMARY KEY (event_id, scoring_version),
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_prediction_eligibility_queue
    ON prediction_event_eligibility(scoring_version, selected, total_score);

CREATE TABLE IF NOT EXISTS event_sources (
    source_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    source_role TEXT NOT NULL CHECK (source_role IN ('primary', 'related')),
    title TEXT,
    source_url TEXT,
    source_notice_code TEXT,
    document_fetch_ok INTEGER CHECK (document_fetch_ok IN (0, 1)),
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_sources_event
    ON event_sources(event_id, source_role);

CREATE TABLE IF NOT EXISTS model_runs (
    run_id TEXT PRIMARY KEY,
    run_name TEXT NOT NULL,
    model_version TEXT,
    prompt_version TEXT,
    code_revision TEXT,
    status TEXT NOT NULL,
    config_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL,
    up_score REAL,
    down_score REAL,
    rationale TEXT,
    evidence_graph_json TEXT,
    trajectory_path TEXT,
    predicted_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES model_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (event_id) REFERENCES events(event_id),
    UNIQUE (run_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_predictions_event
    ON predictions(event_id, predicted_at);

CREATE TABLE IF NOT EXISTS labels (
    label_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    asset_return REAL,
    benchmark_return REAL,
    abnormal_return REAL,
    direction TEXT,
    threshold REAL,
    price_source TEXT,
    available_at TEXT NOT NULL,
    label_json TEXT,
    FOREIGN KEY (event_id) REFERENCES events(event_id),
    UNIQUE (event_id, horizon)
);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    run_id TEXT,
    category TEXT NOT NULL,
    verdict TEXT,
    notes TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(event_id),
    FOREIGN KEY (run_id) REFERENCES model_runs(run_id)
);

CREATE VIEW IF NOT EXISTS v_offline_evaluation_queue AS
SELECT * FROM events
WHERE evaluation_status = 'strict';

CREATE VIEW IF NOT EXISTS v_review_queue AS
SELECT * FROM events
WHERE evaluation_status = 'review';

CREATE VIEW IF NOT EXISTS v_online_prediction_queue AS
SELECT * FROM events
WHERE evaluation_status = 'strict' AND prediction_eligible = 1;

CREATE VIEW IF NOT EXISTS v_offline_direction_prediction_queue_ge70 AS
SELECT e.*, q.total_score, q.score_breakdown_json, q.assessment_json
FROM prediction_event_eligibility AS q
JOIN events AS e ON e.event_id = q.event_id
WHERE q.selected = 1 AND q.dataset_scope = 'offline_backfill';

CREATE VIEW IF NOT EXISTS v_scored_predictions AS
SELECT
    p.run_id,
    p.event_id,
    e.market,
    e.symbol,
    e.event_type_l2,
    p.direction AS predicted_direction,
    p.confidence,
    l.horizon,
    l.direction AS label_direction,
    l.abnormal_return,
    CASE
        WHEN l.direction IS NULL THEN NULL
        WHEN p.direction = l.direction THEN 1
        ELSE 0
    END AS is_correct
FROM predictions AS p
JOIN events AS e ON e.event_id = p.event_id
LEFT JOIN labels AS l ON l.event_id = p.event_id;

CREATE VIEW IF NOT EXISTS v_cohort_funnel AS
SELECT
    cohort_date,
    raw_count,
    candidate_count,
    validated_count,
    quarantine_count,
    prediction_eligible_count,
    CASE WHEN raw_count = 0 THEN 0.0
         ELSE CAST(validated_count AS REAL) / raw_count END AS packet_pass_rate
FROM cohorts;

CREATE VIEW IF NOT EXISTS v_all_event_catalog AS
SELECT
    c.*,
    s.source,
    s.source_key,
    s.issuer_name,
    s.announcement_date,
    s.title,
    s.source_url
FROM classified_event_catalog AS c
JOIN source_records AS s ON s.record_id = c.source_record_id;

CREATE VIEW IF NOT EXISTS v_prediction_candidate_catalog AS
SELECT * FROM v_all_event_catalog
WHERE catalog_role = 'prediction_candidate';

CREATE VIEW IF NOT EXISTS v_event_catalog_review_queue AS
SELECT * FROM v_all_event_catalog
WHERE catalog_role = 'review';

CREATE VIEW IF NOT EXISTS v_evidence_only_catalog AS
SELECT * FROM v_all_event_catalog
WHERE catalog_role = 'evidence_only';

CREATE VIEW IF NOT EXISTS v_metadata_direction_candidate_queue AS
SELECT c.*, s.total_score, s.score_breakdown_json, s.screening_json
FROM metadata_event_screening AS s
JOIN v_all_event_catalog AS c ON c.catalog_event_id = s.catalog_event_id
WHERE s.selected = 1;
