import os
import sys
import tempfile

import pytest

# Every test module shares one throwaway SQLite database. This must be set before any app
# module is imported, or db.DB_PATH would point at the real jobs.db.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("SKIP_STARTUP_RESCORE", "1")
# Production hashing is deliberately slow; tests make many accounts
os.environ.setdefault("PASSWORD_HASH_ROUNDS", "1000")
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))



@pytest.fixture(autouse=True)
def _csrf_off_by_default(request):
    """Most tests drive the API directly; tests/test_security.py turns CSRF back on."""
    if "csrf" in request.keywords:
        yield
        return
    import dashboard
    dashboard.app.config["CSRF_ENABLED"] = False
    yield
    dashboard.app.config["CSRF_ENABLED"] = True


@pytest.fixture(autouse=True)
def _offline_dns(request, monkeypatch):
    """Outbound fetches check where a host points (security.check_public_url). Tests run offline
    with fake pages, so every name resolves to a public documentation address unless a test says otherwise."""
    if "real_dns" in request.keywords:
        yield
        return
    import security
    monkeypatch.setattr(security, "_resolve", lambda host, port: ["93.184.216.34"])
    yield
