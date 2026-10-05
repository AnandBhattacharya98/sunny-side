import hashlib
import hmac
import binascii
import os
import re
import sqlite3
from datetime import datetime

PBKDF2_ROUNDS = 100000
MIN_PASSWORD_LENGTH = 8
USERNAME_PATTERN = re.compile(r"^[a-z0-9._@+-]{3,64}$")


def hash_password(password: str) -> str:
    # Fresh random salt per password. Stored as hex(salt + b":" + key), the same
    # layout older hashes used, so verify_password handles both.
    salt = binascii.hexlify(os.urandom(16))
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return binascii.hexlify(salt + b":" + key).decode("ascii")

def verify_password(password: str, hashed: str) -> bool:
    try:
        raw = binascii.unhexlify(hashed.encode("ascii"))
        salt, original_key = raw.split(b":", 1)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
        return hmac.compare_digest(key, original_key)
    except Exception:
        return False

def validate_new_credentials(username: str, password: str) -> None:
    if not username or not password:
        raise ValueError("Username and password cannot be empty")
    if not USERNAME_PATTERN.match(username):
        raise ValueError("Username must be 3-64 characters: letters, numbers, and . _ @ + -")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

def signup_user(conn: sqlite3.Connection, username: str, password: str, validate: bool = True) -> int:
    username = username.strip().lower()
    if validate:
        validate_new_credentials(username, password)
    elif not username or not password:
        raise ValueError("Username and password cannot be empty")

    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        raise ValueError("Username already exists")

    p_hash = hash_password(password)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, p_hash, datetime.now().isoformat())
    )
    conn.commit()
    return cur.lastrowid

def login_user(conn: sqlite3.Connection, username: str, password: str) -> dict:
    username = username.strip().lower()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    user = dict(row)
    if verify_password(password, user["password_hash"]):
        # Upgrade hashes made with the old shared salt
        if user["password_hash"].startswith(binascii.hexlify(b"default_salt_123:").decode()):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
            conn.commit()
        return user
    return None
