"""
crypto_util.py — App secret resolution and at-rest encryption for user credentials
(Gmail app passwords, personal Gemini keys).

Set FLASK_SECRET_KEY in production. If it's missing, a random key is generated once
and saved next to the database so sessions survive restarts on the same machine,
but anything encrypted with it becomes unreadable if that file is lost.
Optionally set DATA_ENCRYPTION_KEY (a Fernet key) to encrypt stored credentials
with a key separate from the session secret.
"""

import os
import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

ENC_PREFIX = "enc:"
_base_dir = os.path.dirname(os.path.abspath(__file__))
_SECRET_FILE = os.path.join(_base_dir, ".secret_key")

_cached_secret = None
_cached_fernet = None


def get_app_secret() -> str:
    global _cached_secret
    if _cached_secret:
        return _cached_secret
    key = os.getenv("FLASK_SECRET_KEY", "").strip()
    if not key:
        try:
            fd = os.open(_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            key = secrets.token_hex(32)
            with os.fdopen(fd, "w") as f:
                f.write(key)
        except FileExistsError:
            # Another worker (or an earlier run) already created it
            for _ in range(20):
                with open(_SECRET_FILE) as f:
                    key = f.read().strip()
                if key:
                    break
                import time
                time.sleep(0.05)
        if not key:
            raise RuntimeError("Could not establish an app secret; set FLASK_SECRET_KEY.")
        print("WARNING: FLASK_SECRET_KEY is not set. Using a generated key stored in .secret_key; "
              "set FLASK_SECRET_KEY in production so logins and saved credentials survive redeploys.")
    _cached_secret = key
    return key


def _fernet() -> Fernet:
    global _cached_fernet
    if _cached_fernet:
        return _cached_fernet
    raw = os.getenv("DATA_ENCRYPTION_KEY", "").strip()
    if raw:
        _cached_fernet = Fernet(raw.encode())
    else:
        digest = hashlib.sha256(("job-hunter-data:" + get_app_secret()).encode()).digest()
        _cached_fernet = Fernet(base64.urlsafe_b64encode(digest))
    return _cached_fernet


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith(ENC_PREFIX):
        return value
    return ENC_PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    """Returns plaintext. Legacy unencrypted values pass through unchanged."""
    if not value:
        return ""
    if not value.startswith(ENC_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        print("WARNING: could not decrypt a stored credential (encryption key changed?). Treating it as empty.")
        return ""
