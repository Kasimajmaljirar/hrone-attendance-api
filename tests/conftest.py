"""Shared fixtures.

Two kinds of tests live in this folder:
  * MOCKED  - test_rules.py (pure functions) and test_api_mocked.py (the API on top of `mongomock`, an in-memory
              imitation of MongoDB). Fast, no setup, but mongomock cannot run $setWindowFields / $range / explain.
  * REAL    - test_real_mongo.py. Runs the analytics pipelines and explain against a real MongoDB 6+.
              Skipped unless MONGO_TEST_URI is set.
"""
import os
import sys

import mongomock
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))

from app import main  # noqa: E402


@pytest.fixture()
def mock_client():
    """A fresh in-memory database patched into the app for every test (isolated, no personal DB involved)."""
    fake = mongomock.MongoClient(tz_aware=True)
    main.client, main.db = fake, fake["test_attendance"]
    main._indexes_ready = False
    with TestClient(main.app) as test_client:  # entering the context runs the startup (index creation)
        yield test_client


# Epoch-millisecond constants (IST = UTC+05:30). 2026-07-06 is a Monday.
IN_1005 = 1_783_312_500_000          # 2026-07-06 10:05:00 IST  (35 minutes after a 09:30 shift start)
H = 3_600_000


def make_employee(client, code="EMP0001", **overrides):
    body = {"emp_code": code, "name": "Asha Rao", "email": "asha@example.com", "department": "Engineering",
            "joined_on": "2026-01-05", **overrides}
    response = client.post("/employees", json=body)
    assert response.status_code == 201, response.text
    return response.json()
