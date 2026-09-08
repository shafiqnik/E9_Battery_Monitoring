import os
import sys

CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.txt")


def load_credentials(path=None):
    path = path or CREDENTIALS_FILE
    if not os.path.isfile(path):
        sys.exit(f"Missing {path}. Create it with username=... and password=...")
    values = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip().lower()] = val.strip().strip('"').strip("'")
    user = values.get("username") or values.get("login") or values.get("user")
    password = values.get("password")
    if not user or password is None or password == "":
        sys.exit(f"{path} must contain username=... and password=...")
    return user, password
