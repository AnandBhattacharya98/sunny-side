import hashlib
import binascii
import sqlite3
from datetime import datetime

SALT = b"default_salt_123"

def hash_password(password: str) -> str:
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), SALT, 100000)
    return binascii.hexlify(SALT + b":" + key).decode("ascii")

def verify_password(password: str, hashed: str) -> bool:
    try:
        parts = binascii.unhexlify(hashed.encode("ascii")).split(b":")
        salt = parts[0]
        original_key = parts[1]
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        return key == original_key
    except Exception:
        return False

def signup_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    username = username.strip().lower()
    if not username or not password:
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
        return user
    return None
