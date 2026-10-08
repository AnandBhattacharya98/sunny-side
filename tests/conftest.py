import os
import sys
import tempfile

import pytest

# Every test module shares one throwaway SQLite database. This must be set before any app
# module is imported, or db.DB_PATH would point at the real jobs.db.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("SKIP_STARTUP_RESCORE", "1")
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
