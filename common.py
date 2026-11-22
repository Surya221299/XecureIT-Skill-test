import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet
import psycopg
from psycopg.rows import dict_row
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer

logger = logging.getLogger("documents")
redis = Redis.from_url(os.environ["REDIS_URL"])
queue = Queue("documents", connection=redis, default_timeout=180, serializer=JSONSerializer)
MAX_RETRIES = 3
cipher = Fernet(os.environ["FILE_KEY"])
storage = Path(os.environ["STORAGE_DIR"])


def database():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def audit(db, document_id, actor, event, detail=None):
    db.execute(
        "INSERT INTO document_audit(document_id, actor, event, detail) VALUES (%s,%s,%s,%s)",
        (document_id, actor, event, detail),
    )
    logger.info("document=%s actor=%s event=%s detail=%s", document_id, actor, event, detail)
