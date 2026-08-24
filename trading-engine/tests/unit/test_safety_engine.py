"""Unit tests for SafetyEngine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from algocrypto.safety.engine import SafetyEngine, SafetyState


def _engine(**overrides):
  cfg = {
    "safety": {
      "quote_stale_soft_seconds": 30,
      "quote_stale_hard_seconds": 90,
      "candle_stale_hard_seconds": 180,
      "block_entries_when_degraded": True,
      **overrides,
    }
  }
  return SafetyEngine(cfg)


def test_normal_when_fresh():
  now = datetime.now(tz=timezone.utc)
  snap = _engine().evaluate(
    last_spot_quote_ts=now - timedelta(seconds=5),
    last_option_quote_ts=now - timedelta(seconds=5),
    last_candle_ts=now - timedelta(seconds=30),
    candles_stale=False,
    broker_connected=True,
    db_ok=True,
    now=now,
  )
  assert snap.state == SafetyState.NORMAL
  assert snap.allow_entries is True


def test_degraded_soft_stale_blocks_entries():
  now = datetime.now(tz=timezone.utc)
  snap = _engine().evaluate(
    last_spot_quote_ts=now - timedelta(seconds=45),
    last_option_quote_ts=now - timedelta(seconds=5),
    last_candle_ts=now - timedelta(seconds=30),
    candles_stale=False,
    broker_connected=True,
    db_ok=True,
    now=now,
  )
  assert snap.state == SafetyState.DEGRADED
  assert snap.allow_entries is False
  assert "spot_quote_stale_soft" in snap.reasons


def test_hard_stale_halts():
  now = datetime.now(tz=timezone.utc)
  snap = _engine().evaluate(
    last_spot_quote_ts=now - timedelta(seconds=120),
    last_option_quote_ts=now - timedelta(seconds=5),
    last_candle_ts=now - timedelta(seconds=30),
    candles_stale=False,
    broker_connected=True,
    db_ok=True,
    now=now,
  )
  assert snap.state == SafetyState.HALTED
  assert snap.allow_entries is False
  assert "spot_quote_stale_hard" in snap.reasons


def test_broker_down_halts():
  now = datetime.now(tz=timezone.utc)
  snap = _engine().evaluate(
    last_spot_quote_ts=now,
    last_option_quote_ts=now,
    last_candle_ts=now,
    candles_stale=False,
    broker_connected=False,
    db_ok=True,
    now=now,
  )
  assert snap.state == SafetyState.HALTED
  assert "broker_unavailable" in snap.reasons
