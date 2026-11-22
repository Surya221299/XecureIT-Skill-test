CREATE TABLE documents (
    id uuid PRIMARY KEY,
    owner_id text NOT NULL,
    mime text NOT NULL,
    size_bytes integer NOT NULL,
    status text NOT NULL DEFAULT 'uploaded'
        CHECK (status IN ('uploaded','queued','processing','retrying','completed','dead_lettered')),
    job_id text UNIQUE,
    category text,
    risk text,
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);
CREATE INDEX documents_active_owner ON documents(owner_id) WHERE deleted_at IS NULL;
CREATE TABLE document_contents (
    document_id uuid PRIMARY KEY REFERENCES documents(id),
    redacted_text text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', redacted_text)) STORED
);
CREATE INDEX contents_search ON document_contents USING GIN(search_vector);
CREATE TABLE pii_findings (
    document_id uuid REFERENCES documents(id),
    kind text NOT NULL,
    confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    occurrences integer NOT NULL,
    PRIMARY KEY (document_id, kind)
);
CREATE TABLE document_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES documents(id),
    actor text NOT NULL,
    event text NOT NULL,
    detail text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE dlq_records (
    job_id text PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES documents(id),
    attempts integer NOT NULL,
    error_code text NOT NULL,
    failed_at timestamptz NOT NULL DEFAULT now()
);
