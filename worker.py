import logging

from rq import Worker, get_current_job
from rq.serializers import JSONSerializer

from common import MAX_RETRIES, audit, cipher, database, logger, queue, redis, storage
from processing import ProcessingError, classify, extract, redact

logger.setLevel(logging.INFO)


def analyze(document_id):
    try:
        process_document(document_id)
    except ProcessingError:
        raise
    except Exception:
        raise ProcessingError("PROCESSING_FAILED") from None


def process_document(document_id):
    job = get_current_job()
    with database() as db:
        doc = db.execute("SELECT * FROM documents WHERE id=%s FOR UPDATE", (document_id,)).fetchone()
        if not doc or doc["deleted_at"] or doc["job_id"] != job.id or doc["status"] == "completed":
            return
        db.execute("UPDATE documents SET status='processing' WHERE id=%s", (document_id,))
        audit(db, document_id, "worker", "processing")

    text = extract(cipher.decrypt((storage / document_id).read_bytes()), doc["mime"])
    cleaned, findings = redact(text)
    category, risk = classify(cleaned, findings)
    with database() as db:
        active = db.execute(
            """SELECT id FROM documents WHERE id=%s AND job_id=%s AND deleted_at IS NULL
               AND status <> 'completed' FOR UPDATE""",
            (document_id, job.id),
        ).fetchone()
        if not active:
            return
        db.execute(
            """INSERT INTO document_contents(document_id, redacted_text) VALUES (%s,%s)
               ON CONFLICT(document_id) DO UPDATE SET redacted_text=excluded.redacted_text""",
            (document_id, cleaned),
        )
        db.execute("DELETE FROM pii_findings WHERE document_id=%s", (document_id,))
        for kind, confidence, count in findings:
            db.execute(
                "INSERT INTO pii_findings VALUES (%s,%s,%s,%s)",
                (document_id, kind, confidence, count),
            )
        db.execute(
            "UPDATE documents SET status='completed', category=%s, risk=%s WHERE id=%s",
            (category, risk, document_id),
        )
        audit(db, document_id, "worker", "completed")


def record_failure(job, connection, exc_type, exc_value, traceback):
    document_id = job.args[0]
    retrying = bool(job.retries_left)
    code = str(exc_value) if isinstance(exc_value, ProcessingError) else "PROCESSING_FAILED"
    failed_with_code(job, document_id, retrying, code)


def failed_with_code(job, document_id, retrying, code):
    with database() as db:
        status = "retrying" if retrying else "dead_lettered"
        changed = db.execute(
            """UPDATE documents SET status=%s WHERE id=%s AND job_id=%s AND deleted_at IS NULL
               AND status NOT IN ('completed','dead_lettered') RETURNING id""",
            (status, document_id, job.id),
        ).fetchone()
        if not changed:
            return
        audit(db, document_id, "worker", status, code)
        if not retrying:
            db.execute(
                "INSERT INTO dlq_records(job_id, document_id, attempts, error_code) VALUES (%s,%s,%s,%s)",
                (job.id, document_id, MAX_RETRIES + 1, code),
            )
            redis.xadd("documents-dlq", {"job_id": job.id, "document_id": document_id, "error_code": code})


def record_worker_exit(job, child_pid, status, usage):
    failed_with_code(job, job.args[0], bool(job.retries_left), "WORKER_EXIT")


if __name__ == "__main__":
    Worker([queue], connection=redis, serializer=JSONSerializer,
           work_horse_killed_handler=record_worker_exit).work()
