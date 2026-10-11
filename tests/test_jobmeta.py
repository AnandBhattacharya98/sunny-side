"""Remote and salary facts read from a job's own text (board filters and card chips)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobmeta  # noqa: E402


def test_salary_formats():
    cases = {
        "CTC: 12 - 18 LPA based on experience": "12 - 18 LPA",
        "Salary ₹25 lakhs per annum": "₹25 lakhs",
        "Base pay $90k - $120k": "$90k - $120k",
        "Compensation: INR 15,00,000 - 20,00,000": "INR 15,00,000 - 20,00,000",
        "USD 80,000 - 100,000 per year": "USD 80,000 - 100,000",
    }
    for text, want in cases.items():
        assert jobmeta.extract_salary(text) == want, text


def test_numbers_that_are_not_pay():
    for text in ("We raised $20M last year.", "5+ years of experience, team of 12 - 18 people", ""):
        assert jobmeta.extract_salary(text) == ""


def test_remote_detection():
    assert jobmeta.is_remote("Remote, India")
    assert jobmeta.is_remote("Bengaluru", "PM", "This is a fully remote role.")
    assert not jobmeta.is_remote("Pune", "PM", "Office based. No remote work.")
    assert not jobmeta.is_remote("Mumbai", "Product Manager", "Join our Mumbai office.")


def test_annotate_sets_both_fields():
    job = jobmeta.annotate({"title": "PM (Remote)", "location": "India", "description": "Package: 30 LPA"})
    assert job["is_remote"] and job["salary_text"] == "30 LPA"
