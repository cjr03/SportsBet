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


@pytest.fixture
def conn(tmp_path):
    from gridiron_edge.db import connect

    c = connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def mk():
    from gridiron_edge.types import KalshiMarket

    def make(ticker, event, mtype, team=None, line=None, yes_bid=None, yes_ask=0.5,
             no_ask=None, yes_ask_size=1000, no_ask_size=1000, status="active", game_id="g"):
        if yes_bid is None:
            yes_bid = yes_ask - 0.01
        if no_ask is None:
            no_ask = 1 - yes_bid
        return KalshiMarket(
            ticker=ticker, event_ticker=event, series="S", market_type=mtype,
            game_id=game_id, team=team, line=line, player=None, stat=None,
            yes_bid=yes_bid, yes_ask=yes_ask, no_bid=1 - yes_ask, no_ask=no_ask,
            yes_ask_size=yes_ask_size, no_ask_size=no_ask_size, status=status,
            result="", fee_multiplier=1.0,
            fetched_at=datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc),
        )

    return make


@pytest.fixture
def load():
    import json
    from pathlib import Path

    def _load(name):
        return json.loads((Path(__file__).parent / "fixtures" / name).read_text())

    return _load
