"""Tests for funding rate data layer (R0.1)."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime

import pytest

from crypto_bot.core.types import Candle
from crypto_bot.data.funding import (
    FundingEvent,
    FundingRepository,
    HistoricalFundingSource,
)
from crypto_bot.storage.db import Database


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    yield db
    db.close()
    import os
    os.unlink(db_path)


@pytest.fixture
def repo(temp_db):
    return FundingRepository(temp_db)


@pytest.fixture
def sample_events():
    base = 1_700_000_000_000  # 2023-11-14
    return [
        FundingEvent("BTCUSDT", base, 0.0001, 35000.0),
        FundingEvent("BTCUSDT", base + 8 * 3_600_000, -0.00005, 35100.0),
        FundingEvent("BTCUSDT", base + 16 * 3_600_000, 0.0002, 35200.0),
        FundingEvent("ETHUSDT", base, 0.00015, 2000.0),
        FundingEvent("ETHUSDT", base + 8 * 3_600_000, 0.0001, 2010.0),
    ]


def test_upsert_many_idempotent(repo, sample_events):
    """Repeated upsert of same events creates no duplicates."""
    count1 = repo.upsert_many(sample_events)
    count2 = repo.upsert_many(sample_events)
    assert count1 == len(sample_events)
    assert count2 == len(sample_events)

    # Verify only unique events stored
    all_btc = repo.get_events("BTCUSDT", 0, 2_000_000_000_000)
    assert len(all_btc) == 3  # 3 BTC events


def test_get_events_time_window(repo, sample_events):
    """Fetching events respects from_ms/to_ms bounds."""
    repo.upsert_many(sample_events)
    base = 1_700_000_000_000

    # Middle window: only 2nd BTC event
    middle = repo.get_events("BTCUSDT", base + 4 * 3_600_000, base + 12 * 3_600_000)
    assert len(middle) == 1
    assert middle[0].funding_time_ms == base + 8 * 3_600_000

    # Window before all events
    early = repo.get_events("BTCUSDT", 0, base - 1)
    assert early == []

    # Window after all events
    late = repo.get_events("BTCUSDT", base + 24 * 3_600_000, base + 48 * 3_600_000)
    assert late == []


def test_latest_event(repo, sample_events):
    """latest_event returns most recent by funding_time_ms."""
    repo.upsert_many(sample_events)
    latest = repo.latest_event("BTCUSDT")
    assert latest is not None
    assert latest.funding_time_ms == 1_700_000_000_000 + 16 * 3_600_000
    assert latest.funding_rate == 0.0002

    # Non-existent symbol
    assert repo.latest_event("DOGEUSDT") is None


class TestHistoricalFundingSource:
    """Tests for in-memory cache with no-lookahead slicing."""

    def test_events_up_to_no_lookahead(self, repo, sample_events):
        """events_up_to excludes future funding timestamps."""
        repo.upsert_many(sample_events)
        source = HistoricalFundingSource(repo)
        source.load_from_repo("BTCUSDT")

        base = 1_700_000_000_000
        # Anchor at 2nd event timestamp - should see first 2 events
        as_of = base + 8 * 3_600_000
        events = source.events_up_to(as_of, "BTCUSDT")
        assert len(events) == 2
        assert events[-1].funding_time_ms == as_of

        # Anchor between 2nd and 3rd - still only 2 events
        as_of = base + 12 * 3_600_000
        events = source.events_up_to(as_of, "BTCUSDT")
        assert len(events) == 2

        # Anchor at 3rd event - all 3 events
        as_of = base + 16 * 3_600_000
        events = source.events_up_to(as_of, "BTCUSDT")
        assert len(events) == 3

    def test_events_up_to_empty_symbol(self, repo):
        """Unknown symbol returns empty list, not error."""
        source = HistoricalFundingSource(repo)
        events = source.events_up_to(1_700_000_000_000, "UNKNOWN")
        assert events == []

    def test_events_between(self, repo, sample_events):
        """events_between returns events within inclusive window."""
        repo.upsert_many(sample_events)
        source = HistoricalFundingSource(repo)
        source.load_from_repo("BTCUSDT")

        base = 1_700_000_000_000
        events = source.events_between(
            base + 4 * 3_600_000, base + 12 * 3_600_000, "BTCUSDT"
        )
        assert len(events) == 1
        assert events[0].funding_time_ms == base + 8 * 3_600_000

    def test_load_events_direct(self):
        """load_events works without repository (synthetic data)."""
        events = [
            FundingEvent("BTCUSDT", 1_000_000, 0.0001, None),
            FundingEvent("BTCUSDT", 2_000_000, 0.0002, None),
        ]
        source = HistoricalFundingSource(None)
        source.load_events("BTCUSDT", events)

        assert source.is_loaded("BTCUSDT")
        result = source.events_up_to(1_500_000, "BTCUSDT")
        assert len(result) == 1

    def test_is_loaded(self, repo, sample_events):
        """is_loaded reflects cache state."""
        source = HistoricalFundingSource(repo)
        assert not source.is_loaded("BTCUSDT")

        repo.upsert_many(sample_events)
        source.load_from_repo("BTCUSDT")
        assert source.is_loaded("BTCUSDT")

    def test_loaded_symbols(self, repo, sample_events):
        """loaded_symbols returns only symbols with data."""
        repo.upsert_many(sample_events)
        source = HistoricalFundingSource(repo)
        source.load_from_repo("BTCUSDT")
        source.load_from_repo("ETHUSDT")

        loaded = source.loaded_symbols
        assert set(loaded) == {"BTCUSDT", "ETHUSDT"}

    def test_future_events_not_visible(self, repo, sample_events):
        """Future funding events are never returned (no look-ahead invariant)."""
        repo.upsert_many(sample_events)
        source = HistoricalFundingSource(repo)
        source.load_from_repo("BTCUSDT")

        # Anchor at first event - only first event visible
        base = 1_700_000_000_000
        events = source.events_up_to(base, "BTCUSDT")
        assert len(events) == 1
        assert events[0].funding_time_ms == base

        # Even though 3 events exist in cache, future ones are excluded
        assert source._cache["BTCUSDT"][0] == sample_events[:3]  # all 3 cached


def test_funding_event_immutable():
    """FundingEvent is frozen dataclass."""
    ev = FundingEvent("BTCUSDT", 1_000_000, 0.0001, 35000.0)
    with pytest.raises(AttributeError):
        ev.funding_rate = 0.0002  # type: ignore


def test_funding_event_slots():
    """FundingEvent uses slots for memory efficiency."""
    ev = FundingEvent("BTCUSDT", 1_000_000, 0.0001, 35000.0)
    assert hasattr(ev, "__slots__")