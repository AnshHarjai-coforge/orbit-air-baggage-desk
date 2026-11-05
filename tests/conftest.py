"""Shared fixtures for all test files.

Later branches must not edit this file; they define new fixtures in their own test files.
"""

from datetime import datetime

import pytest

# Fixed reference time. No test reads the real clock.
NOW = datetime(2026, 11, 2, 8, 0, 0)

ACTOR = "desk"


@pytest.fixture
def conn(tmp_path):
    """A fresh, initialised SQLite database in its own file for each test."""
    # Imported here so a missing module fails the tests that use it, not conftest itself.
    import db

    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()
