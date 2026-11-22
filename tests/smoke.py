"""Run against a live local Compose instance using its generated .env."""
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

BASE = f"http://localhost:{os.environ.get('API_PORT', '8000')}"
PASSWORD = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines())["DEMO_PASSWORD"]
TOKEN = None


def call(path, code=200, method="GET", data=None, content_type=None, auth=True):
    headers = {}
    if TOKEN and auth:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if content_type:
        headers["Content-Type"] = content_type
    try:
        with urlopen(Request(BASE + path, data=data, headers=headers, method=method), timeout=10) as res:
            assert res.status == code, (res.status, code, path)
            body = res.read()
    except HTTPError as err:
        assert err.code == code, (err.code, code, path, err.read())
        return None
    return json.loads(body) if body else None


def upload(payload, mime="application/pdf", expected=201):
    boundary = uuid4().hex
    start = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"synthetic.pdf\"\r\nContent-Type: {mime}\r\n\r\n").encode()
    return call("/api/documents/upload", expected, "POST",
                start + payload + f"\r\n--{boundary}--\r\n".encode(),
                f"multipart/form-data; boundary={boundary}")


def wait_for(path, status, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        doc = call(path)
        if doc["status"] == status:
            return doc
        time.sleep(1)
    raise AssertionError((status, doc["status"]))


def main():
    global TOKEN
    call("/openapi.json")
    call("/api/documents/risk-report", 401, auth=False)
    TOKEN = call("/auth/token", method="POST", data=urlencode({
        "username": "demo", "password": PASSWORD
    }).encode(), content_type="application/x-www-form-urlencoded")["access_token"]
    upload(SAMPLE, "image/png", 415)
    upload(b"x" * (5 * 1024 * 1024 + 1), expected=413)
    initial_high = call("/api/documents/risk-report")["high"]
    document_id = upload(SAMPLE)["id"]
    try:
        path = f"/api/documents/{document_id}"
        analyze = f"/api/documents/analyze/{document_id}"
        job = call(analyze, 202, "POST")
        assert call(analyze, 202, "POST")["job_id"] == job["job_id"]
        result = wait_for(path, "completed")
        assert "[EMAIL]" in result["text"] and "demo@" not in result["text"], result
        assert result["category"] == "financial" and result["risk"] == "high", result
        assert any(row["id"] == document_id for row in call("/api/documents/search?q=invoice&risk=high"))
        assert call("/api/documents/risk-report")["high"] == initial_high + 1
        call(path, 204, "DELETE")
        call(path, 404)
        assert call("/api/documents/risk-report")["high"] == initial_high
    finally:
        call(f"/api/documents/{document_id}", 204, "DELETE")
    failed_id = upload(b"%PDF-1.4\ninvalid test fixture\n%%EOF\n")["id"]
    try:
        call(f"/api/documents/analyze/{failed_id}", 202, "POST")
        wait_for(f"/api/documents/{failed_id}", "dead_lettered")
    finally:
        call(f"/api/documents/{failed_id}", 204, "DELETE")
    print("API smoke test passed")


if __name__ == "__main__":
    SAMPLE = Path("examples/invoice.pdf").read_bytes()
    main()
