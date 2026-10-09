from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dashboard


def test_utc_iso_converts_naive_server_time_to_utc():
    naive = datetime(2026, 10, 9, 8, 30, 15, 123456)
    expected = naive.astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert dashboard.utc_iso(naive.isoformat()) == expected


def test_utc_iso_keeps_aware_values_and_handles_blanks():
    assert dashboard.utc_iso("2026-10-09T08:30:00+05:30") == "2026-10-09T03:00:00Z"
    assert dashboard.utc_iso("") == ""
    assert dashboard.utc_iso(None) == ""
    assert dashboard.utc_iso("not a date") == ""


def test_spoken_time_uses_viewer_timezone():
    stamp = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc).isoformat()
    assert dashboard._spoken_time(stamp, tz=ZoneInfo("Asia/Kolkata")).endswith("at 8:30 AM")
    assert dashboard._spoken_time(stamp, tz=ZoneInfo("America/New_York")).endswith("at 11:00 PM")


def test_viewer_tz_reads_header_and_ignores_junk():
    with dashboard.app.test_request_context(headers={"X-Timezone": "Asia/Kolkata"}):
        assert dashboard._viewer_tz() == ZoneInfo("Asia/Kolkata")
    for bad in ("", "Not/AZone", "../etc/passwd"):
        with dashboard.app.test_request_context(headers={"X-Timezone": bad}):
            assert dashboard._viewer_tz() is None


def test_template_marks_times_for_local_formatting():
    html = dashboard.app.jinja_env.from_string(
        '<time datetime="{{ v|utc_iso }}">x</time>').render(v="2026-10-09T03:00:00+00:00")
    assert 'datetime="2026-10-09T03:00:00Z"' in html
