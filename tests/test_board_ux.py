"""Board page: on-demand job popups, the card Move menu, and the remote/pay filters."""
import pytest

import dashboard
from db import get_conn, DB_PATH


@pytest.fixture
def client():
    conn = get_conn(DB_PATH)
    for uid in (301, 302):
        conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, name, resume_text) VALUES (?, ?, 'x', ?, 'resume')",
                     (uid, f"ux{uid}", f"UX {uid}"))
    conn.execute("DELETE FROM jobs WHERE user_id IN (301, 302)")
    conn.execute("INSERT INTO jobs (job_id, title, company, location, description, status, ai_score, user_id) VALUES "
                 "('ux_a', 'Product Manager', 'Acme', 'Remote, India', 'Package: 20 - 30 LPA', 'scored', 8, 301), "
                 "('ux_b', 'Analyst', 'Beta', 'Pune', 'Office role.', 'applied', 7, 301), "
                 "('ux_other', 'Secret Role', 'Gamma', 'Delhi', '', 'scored', 9, 302)")
    conn.commit()
    conn.close()
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = 301
        yield c


def test_board_no_longer_renders_every_popup(client):
    html = client.get("/").get_data(as_text=True)
    assert 'data-job-id="ux_a"' in html
    assert 'id="modal-backdrop-ux_a"' not in html
    assert 'id="job-modals"' in html


def test_popup_loads_on_demand_for_own_jobs_only(client):
    res = client.get("/job/ux_a/modal")
    assert res.status_code == 200 and 'id="modal-backdrop-ux_a"' in res.get_data(as_text=True)
    assert client.get("/job/ux_other/modal").status_code == 404


def test_cards_have_move_menu_without_their_own_column(client):
    html = client.get("/").get_data(as_text=True)
    card = html[html.index('data-job-id="ux_b"'):]
    card = card[:card.index("</select>")]
    assert 'onchange="moveJob(\'ux_b\'' in card
    assert '<option value="applied">' not in card and '<option value="offer">' in card


def test_cards_carry_remote_and_pay(client):
    html = client.get("/").get_data(as_text=True)
    a = html[html.index('data-job-id="ux_a"'):][:300]
    b = html[html.index('data-job-id="ux_b"'):][:300]
    assert 'data-remote="1"' in a and 'data-salary="1"' in a
    assert 'data-remote="0"' in b and 'data-salary="0"' in b
    assert "20 - 30 LPA" in html
