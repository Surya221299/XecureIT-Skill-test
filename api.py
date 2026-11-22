from datetime import datetime, timedelta, timezone
import hmac
from io import BytesIO
import logging
import os
from typing import Literal
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel
from redis.exceptions import RedisError
from rq import Retry

from common import MAX_RETRIES, audit, cipher, database, logger, queue, redis, storage
from worker import analyze, record_failure

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 20_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ISSUER = "document-security"
logger.setLevel(logging.INFO)
app = FastAPI(title="AI Document Processing & Security Service")
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/token")
Risk = Literal["low", "medium", "high"]
Category = Literal["financial", "legal", "hr", "technical", "personal"]
State = Literal["uploaded", "queued", "processing", "retrying", "completed", "dead_lettered"]


class UploadResult(BaseModel):
    id: UUID
    mime: str
    status: State


class JobResult(BaseModel):
    id: UUID
    job_id: str
    status: State


class SearchResult(BaseModel):
    id: UUID
    category: Category
    risk: Risk
    created_at: datetime


class Finding(BaseModel):
    kind: str
    confidence: float
    occurrences: int


class DocumentResult(UploadResult):
    size_bytes: int
    created_at: datetime
    category: Category | None
    risk: Risk | None
    text: str | None
    pii_findings: list[Finding]


class RiskReport(BaseModel):
    low: int
    medium: int
    high: int

@app.middleware("http")
async def bound_upload(request, call_next):
    if request.method == "POST" and request.url.path == "/api/documents/upload":
        length = request.headers.get("content-length", "")
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            return JSONResponse({"detail": "Content-Length required"}, status_code=411)
        if int(length) > MAX_BYTES + 65536:
            return JSONResponse({"detail": "Multipart request too large"}, status_code=413)
    return await call_next(request)


RATE_LIMIT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], 60) end
return n
"""


def limit(key, count):
    try:
        used = redis.eval(RATE_LIMIT, 1, f"rate:{key}")
    except RedisError:
        raise HTTPException(503, "Rate limiter unavailable") from None
    if used > count:
        raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "60"})


class Token(BaseModel):
    access_token: str
    token_type: Literal["bearer"]


@app.post("/auth/token", response_model=Token)
def token(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    limit(f"login:{request.client.host}", 10)
    password_ok = hmac.compare_digest(form.password.encode(), os.environ["DEMO_PASSWORD"].encode())
    if form.username != "demo" or not password_ok:
        raise HTTPException(401, "Invalid credentials")
    now = datetime.now(timezone.utc)
    signed = jwt.encode(
        {"sub": "demo", "iat": now, "exp": now + timedelta(minutes=60), "iss": ISSUER},
        os.environ["JWT_SECRET"], algorithm="HS256",
    )
    return {"access_token": signed, "token_type": "bearer"}


def authenticated(token_value: str = Depends(oauth2)):
    try:
        claims = jwt.decode(
            token_value, os.environ["JWT_SECRET"], algorithms=["HS256"], issuer=ISSUER,
            options={"require": ["sub", "iat", "exp", "iss"]},
        )
        owner = claims["sub"]
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"}) from None
    limit(f"user:{owner}", 120)
    return owner


def validate_upload(data, declared):
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File exceeds 5 MiB")
    if data.startswith(b"%PDF-") and b"%%EOF" in data[-2048:]:
        actual = "application/pdf"
    else:
        try:
            with Image.open(BytesIO(data), formats=["PNG", "JPEG"]) as image:
                actual = Image.MIME[image.format]
                if image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) > 1:
                    raise HTTPException(422, "Image exceeds pixel or frame limit")
                image.verify()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
            raise HTTPException(415, "Unsupported or invalid file") from None
    if actual != declared:
        raise HTTPException(415, "Declared MIME does not match file")
    return actual


def owned(db, document_id, owner, lock=False):
    sql = "SELECT * FROM documents WHERE id=%s AND owner_id=%s AND deleted_at IS NULL"
    doc = db.execute(sql + (" FOR UPDATE" if lock else ""), (document_id, owner)).fetchone()
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc


@app.post("/api/documents/upload", status_code=201, response_model=UploadResult)
def upload(file: UploadFile = File(...), owner: str = Depends(authenticated)):
    data = file.file.read(MAX_BYTES + 1)
    mime = validate_upload(data, file.content_type)
    document_id = uuid4()
    target = storage / str(document_id)
    try:
        target.write_bytes(cipher.encrypt(data))
        with database() as db:
            db.execute(
                "INSERT INTO documents(id, owner_id, mime, size_bytes) VALUES (%s,%s,%s,%s)",
                (document_id, owner, mime, len(data)),
            )
            audit(db, document_id, owner, "uploaded")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"id": document_id, "mime": mime, "status": "uploaded"}


@app.post("/api/documents/analyze/{document_id}", status_code=202, response_model=JobResult)
def trigger(document_id: UUID, owner: str = Depends(authenticated)):
    with database() as db:
        doc = owned(db, document_id, owner, lock=True)
        if doc["job_id"]:
            return {"id": document_id, "job_id": doc["job_id"], "status": doc["status"]}
        job_id = uuid4().hex
        db.execute("UPDATE documents SET status='queued', job_id=%s WHERE id=%s", (job_id, document_id))
        audit(db, document_id, owner, "queued")
        try:
            queue.enqueue(analyze, str(document_id), job_id=job_id,
                          retry=Retry(max=MAX_RETRIES), on_failure=record_failure, result_ttl=300)
        except RedisError:
            raise HTTPException(503, "Queue unavailable") from None
    return {"id": document_id, "job_id": job_id, "status": "queued"}


@app.get("/api/documents/search", response_model=list[SearchResult])
def search(q: str = Query("", max_length=200), risk: Risk | None = None,
           limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
           owner: str = Depends(authenticated)):
    with database() as db:
        return db.execute(
            """SELECT d.id, d.category, d.risk, d.created_at
               FROM documents d JOIN document_contents c ON d.id=c.document_id
               WHERE d.owner_id=%s AND d.deleted_at IS NULL
                 AND (%s='' OR c.search_vector @@ plainto_tsquery('simple', %s))
                 AND (%s::text IS NULL OR d.risk=%s)
               ORDER BY d.created_at DESC, d.id LIMIT %s OFFSET %s""",
            (owner, q, q, risk, risk, limit, offset),
        ).fetchall()


@app.get("/api/documents/risk-report", response_model=RiskReport)
def risk_report(owner: str = Depends(authenticated)):
    with database() as db:
        rows = db.execute(
            """SELECT risk, count(*) AS total FROM documents WHERE owner_id=%s
               AND deleted_at IS NULL AND status='completed' GROUP BY risk""",
            (owner,),
        ).fetchall()
    return {"low": 0, "medium": 0, "high": 0} | {r["risk"]: r["total"] for r in rows}


@app.get("/api/documents/{document_id}", response_model=DocumentResult)
def read(document_id: UUID, owner: str = Depends(authenticated)):
    with database() as db:
        doc = owned(db, document_id, owner, lock=True)
        content = db.execute(
            "SELECT redacted_text FROM document_contents WHERE document_id=%s", (document_id,)
        ).fetchone()
        findings = db.execute(
            "SELECT kind, confidence, occurrences FROM pii_findings WHERE document_id=%s ORDER BY kind",
            (document_id,),
        ).fetchall()
        audit(db, document_id, owner, "viewed")
    return {"id": document_id, "mime": doc["mime"], "size_bytes": doc["size_bytes"],
            "created_at": doc["created_at"], "status": doc["status"],
            "category": doc["category"], "risk": doc["risk"],
            "text": content["redacted_text"] if content else None, "pii_findings": findings}


@app.delete("/api/documents/{document_id}", status_code=204)
def delete(document_id: UUID, owner: str = Depends(authenticated)):
    with database() as db:
        doc = db.execute("SELECT * FROM documents WHERE id=%s AND owner_id=%s FOR UPDATE",
                         (document_id, owner)).fetchone()
        if not doc:
            raise HTTPException(404, "Document not found")
        if not doc["deleted_at"]:
            db.execute("UPDATE documents SET deleted_at=now() WHERE id=%s", (document_id,))
            audit(db, document_id, owner, "soft_deleted")
    return Response(status_code=204)
