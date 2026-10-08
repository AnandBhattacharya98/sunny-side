import pytest


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
