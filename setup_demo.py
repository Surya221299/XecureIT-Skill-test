import base64
import os
from pathlib import Path
import secrets

env = Path(".env")
try:
    fd = os.open(env, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    print("Using existing .env")
else:
    with os.fdopen(fd, "w") as file:
        print(f"JWT_SECRET={secrets.token_urlsafe(32)}", file=file)
        print(f"FILE_KEY={base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}", file=file)
        print(f"DEMO_PASSWORD={secrets.token_urlsafe(32)}", file=file)
    print("Credentials are in .env")
