import hashlib
import hmac
import binascii
import os
import re
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash

# Old hashes: hex(salt + b":" + key), PBKDF2-SHA256 with 100k rounds. They still verify and
# are upgraded to the current format the next time their owner signs in.
LEGACY_PBKDF2_ROUNDS = 100000
# Current hashes: werkzeug's "pbkdf2:sha256:<rounds>$salt$hash" (OWASP's 2023 figure is 600k).
PASSWORD_HASH_ROUNDS = int(os.getenv("PASSWORD_HASH_ROUNDS", "600000"))
PASSWORD_HASH_METHOD = f"pbkdf2:sha256:{PASSWORD_HASH_ROUNDS}"
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
USERNAME_PATTERN = re.compile(r"^[a-z0-9._@+-]{3,64}$")

# The passwords attackers try first. Lower-cased; checked on signup and password changes.
COMMON_PASSWORDS = frozenset("""
password password1 password12 password123 password1234 passw0rd p@ssword p@ssw0rd 12345678 123456789
1234567890 0123456789 87654321 11111111 00000000 12341234 11223344 qwertyui qwertyuiop qwerty123
qwerty12 1q2w3e4r 1q2w3e4r5t zaq12wsx asdfghjk asdfasdf iloveyou iloveyou1 sunshine princess
football baseball welcome welcome1 welcome123 letmein1 admin123 admin1234 administrator changeme
trustno1 superman batman123 abcd1234 abc12345 abcdefgh monkey123 dragon123 starwars computer
internet whatever 1qaz2wsx qazwsxedc michael1 jennifer india123 india@123 bharat123 pakistan
sunnyside sunny123 sunnyside1 jobhunter jobhunter1
""".split())


def hash_password(password: str) -> str:
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD)


def _is_legacy_hash(hashed: str) -> bool:
    return bool(hashed) and "$" not in hashed


def needs_rehash(hashed: str) -> bool:
    """True for hashes made with an older scheme or fewer rounds than today's setting."""
    return _is_legacy_hash(hashed) or not (hashed or "").startswith(PASSWORD_HASH_METHOD + "$")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed or password is None or len(password) > MAX_PASSWORD_LENGTH:
        return False
    if not _is_legacy_hash(hashed):
        try:
            return check_password_hash(hashed, password)
        except Exception:
            return False
    try:
        raw = binascii.unhexlify(hashed.encode("ascii"))
        salt, original_key = raw.split(b":", 1)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, LEGACY_PBKDF2_ROUNDS)
        return hmac.compare_digest(key, original_key)
    except Exception:
        return False


# Checked when a username doesn't exist, so a wrong username takes as long as a wrong password
# (otherwise response times would reveal which usernames are real).
_DUMMY_HASH = None


def burn_password_check(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(binascii.hexlify(os.urandom(16)).decode())
    verify_password(password or "", _DUMMY_HASH)


def validate_password(password: str, username: str = "") -> None:
    if not password:
        raise ValueError("Password cannot be empty")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password can be at most {MAX_PASSWORD_LENGTH} characters")
    low = password.lower()
    if low in COMMON_PASSWORDS or len(set(low)) <= 2:
        raise ValueError("That password is too easy to guess. Try a longer phrase, or mix in a few words and numbers.")
    if username and len(username) >= 3 and username.lower() in low:
        raise ValueError("Your password shouldn't contain your username.")


def validate_new_credentials(username: str, password: str) -> None:
    if not username or not password:
        raise ValueError("Username and password cannot be empty")
    if not USERNAME_PATTERN.match(username):
        raise ValueError("Username must be 3-64 characters: letters, numbers, and . _ @ + -")
    validate_password(password, username)

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
        burn_password_check(password)
        return None
    user = dict(row)
    if verify_password(password, user["password_hash"]):
        # Upgrade older, weaker hashes now that we briefly know the password
        if needs_rehash(user["password_hash"]):
            user["password_hash"] = hash_password(password)
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (user["password_hash"], user["id"]))
            conn.commit()
        return user
    return None
