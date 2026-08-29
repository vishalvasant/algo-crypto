"""Decision flow snapshot for crypto cockpit — pipeline stages + execution gates."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from algocrypto.models.events import Bias, FeatureSnapshot, StrategyDecision

StageStatus = str  # ok | warn | block | pending | idle


def _stage(
    *,
    stage_id: str,
    label: str,
    status: StageStatus,
    detail: str,
    value: Any = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": stage_id,
        "label": label,
        "status": status,
        "detail": detail,
    }
    if value is not None:
        row["value"] = value
    return row


def build_decision_flow(
    features: FeatureSnapshot,
    *,
    decision: StrategyDecision | None = None,
    min_confidence: int = 75,
    orderbook_cfg: dict | None = None,
    feed_mode: str = "offline",
    ws_open: bool = False,
    ws_quote_age_sec: float | None = None,
    quote_age_sec: float | None = None,
    entries_blocked: bool = False,
    block_reason: str | None = None,
    auto_trade_enabled: bool = True,
    kill_switch: bool = False,
    has_open_position: bool = False,
    last_entry_block: str | None = None,
) -> dict[str, Any]:
    """Ordered stages: Feed → Spot → Momentum → Router → Orderbook → Entry."""
    ob = orderbook_cfg or {}
    ob_enabled = bool(ob.get("enabled", True))
    min_ask = int(ob.get("min_ask_size_lots", 50))
    cov_mult = float(ob.get("min_ask_coverage_mult", 1.0))
    max_ratio = float(ob.get("max_ask_bid_size_ratio", 8.0))

    spot = features.nifty_spot
    vwap = features.session_vwap
    spot_vs_vwap: str | None = None
    if spot is not None and vwap is not None:
        sf, vf = float(spot), float(vwap)
        if sf > vf:
            spot_vs_vwap = "ABOVE"
        elif sf < vf:
            spot_vs_vwap = "BELOW"
        else:
            spot_vs_vwap = "AT"

    bias = features.bias_5m.value.upper() if features.bias_5m else Bias.NEUTRAL.value.upper()
    momentum_aligned = True
    if spot_vs_vwap == "ABOVE" and bias == "BEARISH":
        momentum_aligned = False
    elif spot_vs_vwap == "BELOW" and bias == "BULLISH":
        momentum_aligned = False

    mode = (feed_mode or "offline").lower()
    feed_stale = ws_quote_age_sec is not None and ws_quote_age_sec > 12
    if mode in ("websocket", "ws"):
        if ws_open and not feed_stale:
            feed_status: StageStatus = "ok"
            feed_detail = "Delta WebSocket live"
        elif ws_open and feed_stale:
            feed_status = "warn"
            feed_detail = f"WS stale · last tick {int(ws_quote_age_sec)}s ago"
        elif quote_age_sec is not None and quote_age_sec <= 15:
            feed_status = "ok"
            feed_detail = f"Quotes live · Delta feed · {int(quote_age_sec)}s ago"
        else:
            feed_status = "warn"
            feed_detail = "WebSocket connecting — REST backup active"
    elif mode == "rest":
        age = f" · {int(quote_age_sec)}s ago" if quote_age_sec is not None else ""
        feed_status = "ok" if quote_age_sec is not None and quote_age_sec <= 30 else "warn"
        feed_detail = f"Delta REST poll{age}"
    else:
        feed_status = "block"
        feed_detail = "Market feed offline"

    conf = int(decision.confidence) if decision and decision.confidence else 0
    strat = (decision.selected_strategy if decision else None) or "NO_TRADE"
    trade_allowed = bool(decision and decision.trade_allowed and strat not in ("NO_TRADE", ""))

    if trade_allowed:
        router_status: StageStatus = "ok"
        router_detail = (
            f"{strat.replace('_', ' ')} · {decision.position_side} · conf {conf}% "
            f"(min {min_confidence}%)"
        )
    elif strat == "NO_TRADE" and decision and decision.selected_reason:
        router_status = "pending" if conf >= min_confidence * 0.85 else "block"
        router_detail = decision.selected_reason.replace("_", " ")[:72]
    else:
        router_status = "pending"
        router_detail = f"Waiting · min confidence {min_confidence}%"

    ob_detail = (
        f"min ask {min_ask} lots · depth ≥{cov_mult:.1f}× size · "
        f"ask/bid ≤{max_ratio:.0f}×"
    )
    if not ob_enabled:
        ob_status: StageStatus = "ok"
        ob_detail = "Orderbook gate disabled"
    elif last_entry_block and "orderbook" in last_entry_block.lower():
        ob_status = "block"
        ob_detail = f"{last_entry_block.replace('_', ' ')} · limits: {ob_detail}"
    elif trade_allowed:
        ob_status = "warn"
        ob_detail = f"Checked on entry · {ob_detail}"
    else:
        ob_status = "pending"
        ob_detail = f"On entry · {ob_detail}"

    if has_open_position:
        entry_status: StageStatus = "ok"
        entry_detail = "Position open — exit rules active"
    elif kill_switch:
        entry_status = "block"
        entry_detail = "Kill switch ON"
    elif not auto_trade_enabled:
        entry_status = "block"
        entry_detail = "Auto trade OFF"
    elif entries_blocked:
        entry_status = "block"
        entry_detail = (block_reason or "entries blocked").replace("_", " ")
    elif last_entry_block:
        entry_status = "block"
        entry_detail = last_entry_block.replace("_", " ")[:72]
    elif trade_allowed:
        entry_status = "ok"
        entry_detail = "Signal passed router — executing gates"
    else:
        entry_status = "pending"
        entry_detail = "Waiting for router signal ≥ min confidence"

    stages = [
        _stage(
            stage_id="feed",
            label="Data feed",
            status=feed_status,
            detail=feed_detail,
            value=mode,
        ),
        _stage(
            stage_id="spot",
            label="BTC spot",
            status="ok" if spot is not None else "warn",
            detail=f"${float(spot):,.2f}" if spot is not None else "Spot quote pending",
            value=spot,
        ),
        _stage(
            stage_id="momentum",
            label="Momentum + confirm",
            status="ok" if momentum_aligned else "block",
            detail=f"Spot {spot_vs_vwap or '—'} VWAP · 5m {bias.lower()}",
            value=spot_vs_vwap,
        ),
        _stage(
            stage_id="router",
            label="Strategy router",
            status=router_status,
            detail=router_detail,
            value=strat,
        ),
        _stage(
            stage_id="orderbook",
            label="Orderbook gate",
            status=ob_status,
            detail=ob_detail,
            value=max_ratio,
        ),
        _stage(
            stage_id="entry",
            label="Final entry",
            status=entry_status,
            detail=entry_detail,
            value=block_reason,
        ),
    ]

    return {
        "stages": stages,
        "min_confidence": min_confidence,
        "router_confidence": conf if conf else None,
        "selected_strategy": strat,
        "trade_allowed": trade_allowed,
        "spot_vs_vwap": spot_vs_vwap,
        "bias_5m": bias,
        "momentum_aligned": momentum_aligned,
        "orderbook_gates": {
            "enabled": ob_enabled,
            "min_ask_size_lots": min_ask,
            "min_ask_coverage_mult": cov_mult,
            "max_ask_bid_size_ratio": max_ratio,
        },
        "last_entry_block": last_entry_block,
        "feed_mode": mode,
        "ws_open": ws_open,
        "ws_quote_age_sec": ws_quote_age_sec,
        "quote_age_sec": quote_age_sec,
    }
