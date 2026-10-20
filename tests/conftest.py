from dataclasses import replace
from datetime import datetime, timezone

import pytest

from gridiron_edge.config import Settings


@pytest.fixture
def settings(tmp_path):
    return replace(
        Settings(),
        odds_api_key="test-odds-key",
        cfbd_api_key="test-cfbd-key",
        therundown_api_key="test-rundown-key",
        notify_phone="+15555550100",
        db_path=tmp_path / "t.db",
    )


@pytest.fixture
def now():
    return datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)
