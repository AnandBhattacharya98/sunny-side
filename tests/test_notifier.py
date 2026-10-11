"""Outgoing email transport. Run with: python -m pytest tests/"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notifier  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code, self.text = status_code, text


def _configure(monkeypatch, brevo="", password=""):
    monkeypatch.setattr(notifier, "SENDER_EMAIL", "alerts@example.com")
    monkeypatch.setattr(notifier, "BREVO_API_KEY", brevo)
    monkeypatch.setattr(notifier, "SENDER_PASSWORD", password)


def test_not_configured_without_a_way_to_send(monkeypatch):
    _configure(monkeypatch)
    assert not notifier.email_configured()
    assert notifier.send_email("a@example.com", "Hi", "<p>hi</p>") is False


def test_brevo_key_alone_is_enough(monkeypatch):
    _configure(monkeypatch, brevo="xkeysib-test")
    assert notifier.email_configured()


def test_brevo_is_used_over_smtp(monkeypatch):
    _configure(monkeypatch, brevo="xkeysib-test", password="gmail-app-pw")
    calls = []
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw)) or FakeResponse(201))
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SMTP used")))
    assert notifier.send_email("a@example.com", "Reset", "<p>link</p>") is True
    url, kw = calls[0]
    assert url == "https://api.brevo.com/v3/smtp/email"
    assert kw["headers"]["api-key"] == "xkeysib-test"
    assert kw["json"]["to"] == [{"email": "a@example.com"}]
    assert kw["json"]["sender"]["email"] == "alerts@example.com"
    assert kw["json"]["htmlContent"] == "<p>link</p>"


def test_brevo_error_reports_failure(monkeypatch):
    _configure(monkeypatch, brevo="xkeysib-test")
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse(401, "unauthorized"))
    assert notifier.send_email("a@example.com", "Reset", "<p>link</p>") is False


def test_gmail_smtp_without_brevo(monkeypatch):
    _configure(monkeypatch, password="gmail-app-pw")
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, user, pw): sent.append(("login", user, pw))
        def sendmail(self, frm, to, body): sent.append(("send", frm, to))

    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    assert notifier.send_email("a@example.com", "Reset", "<p>link</p>") is True
    assert sent == [("login", "alerts@example.com", "gmail-app-pw"), ("send", "alerts@example.com", "a@example.com")]
