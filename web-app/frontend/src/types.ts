export type DecisionFlowStageStatus = "ok" | "warn" | "block" | "pending" | "idle";

export interface DecisionFlowStage {
  id: string;
  label: string;
  detail: string;
  status: DecisionFlowStageStatus;
  value?: string | number | null;
}

export interface DecisionFlowOrderbookGates {
  enabled: boolean;
  min_ask_size_lots: number;
  min_ask_coverage_mult: number;
  max_ask_bid_size_ratio: number;
}

export interface DecisionFlowSnapshot {
  stages: DecisionFlowStage[];
  min_confidence?: number;
  router_confidence?: number | null;
  selected_strategy?: string;
  trade_allowed?: boolean;
  spot_vs_vwap?: string | null;
  bias_5m?: string | null;
  momentum_aligned?: boolean;
  orderbook_gates?: DecisionFlowOrderbookGates;
  last_entry_block?: string | null;
  feed_mode?: string;
  ws_open?: boolean;
  ws_quote_age_sec?: number | null;
  quote_age_sec?: number | null;
}

export interface MarketSummary {
  underlying: string;
  spot_ltp: number | null;
  session_vwap: number | null;
  spot_vs_vwap: string | null;
  atm_strike: number | null;
  bias_5m: string;
  market_session: string;
  market_open: boolean;
  strategy: string;
  trading_mode: string;
  ist_time?: string;
  today_pnl: number;
  trade_count: number;
  starting_capital?: number;
  available_capital?: number;
  deployed_capital?: number;
  used_margin?: number;
  has_open_position?: boolean;
  auto_trading_active?: boolean;
  auto_trade_enabled?: boolean;
  scan_interval_seconds?: number;
  candidate_count?: number;
  rejection_count?: number;
  consecutive_losses?: number;
  is_expiry_day?: boolean;
  kill_switch?: boolean;
  entries_blocked?: boolean;
  block_reason?: string | null;
  feed_mode?: string;
  expiry_symbol?: string | null;
  instrument_count?: number;
  recent_rejections?: Array<{ tsym: string; reasons: string[] }>;
  open_position?: {
    tsym: string;
    quantity: number;
    entry_price: number;
    current_ltp?: number;
    unrealized_pnl?: number;
    side?: string;
    setup_type?: string;
    premium_deployed?: number;
  };
  open_positions?: Array<{
    tsym: string;
    side: string;
    quantity: number;
    entry_price: number;
    current_ltp: number;
    unrealized_pnl: number;
    premium_deployed: number;
    setup_type: string;
  }>;
  open_position_count?: number;
  unrealized_pnl?: number;
  equity?: number;
  unread_notifications?: number;
  ws_open?: boolean;
  ws_quote_age_sec?: number | null;
  quote_age_sec?: number | null;
  bullish_confidence?: number | null;
  bearish_confidence?: number | null;
  router_confidence?: number | null;
  confidence?: number | null;
  levels?: LevelMapSummary | null;
  risk_dashboard?: {
    realized_pnl?: number;
    unrealized_pnl?: number;
    total_pnl?: number;
    loss_budget_pct?: number;
    margin_deploy_pct?: number;
    risk_pct?: number;
    risk_driver?: "none" | "margin" | "loss_budget";
    has_open_position?: boolean;
    entry_status?: string;
  };
  commodities?: CommoditySnapshot[];
  broker_realized_pnl?: number;
  today_realized_pnl?: number;
  broker_unrealized_pnl?: number;
  broker_day_pnl?: number;
  max_daily_loss?: number;
  max_deployed_pct_of_equity?: number;
  decision_flow?: DecisionFlowSnapshot | null;
  net_unrealized_pnl?: number | null;
  delta_contract_spec?: {
    btc_lot_size?: number;
    eth_lot_size?: number;
    note?: string;
  };
  delta_fees_model?: {
    options_taker_pct?: number;
    options_maker_pct?: number;
    premium_cap_pct?: number;
    gst_pct?: number;
    formula?: string;
  };
}

export interface CommoditySnapshot {
  underlying: string;
  display_name?: string;
  spot_ltp?: number | null;
  trading_spot_ltp?: number | null;
  atm_strike?: number | null;
  atm_strike_steps?: number;
  strike_step?: number;
  items?: WatchlistItem[];
}

export interface OhlcBar {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
}

export interface ChartCandlesResponse {
  underlying: string;
  interval: string;
  price_source?: string;
  instrument_token?: string | null;
  fut_tsym?: string | null;
  bars: OhlcBar[];
}

export interface LevelMapSummary {
  pdh?: number | null;
  pdl?: number | null;
  orh?: number | null;
  orl?: number | null;
  vwap?: number | null;
  supports?: number[];
  resistances?: number[];
  nearest_support?: number | null;
  nearest_resistance?: number | null;
  level_labels?: Record<string, number>;
  rejection_setup?: string | null;
}

export interface PositionStreamPayload {
  open_positions?: WatchlistOpenPosition[];
  feed_mode?: string;
  ws_open?: boolean;
  ts?: string;
  error?: string;
}

export type Severity = "info" | "warning" | "critical";

export interface BrokerSession {
  user_id: string;
  source: string;
  expires_at: string | null;
  valid: boolean;
  env?: string;
}

export interface EngineHealth {
  status: string;
  trading_mode: string;
  db_ok: boolean;
  broker_connected: boolean;
  broker_session?: BrokerSession | null;
  flattrade_session?: BrokerSession | null;
  spot_ltp: string | null;
  instrument_count: number;
  last_quote_ts: string | null;
  kill_switch?: boolean;
  ts: string;
  error?: string;
}

export interface Notification {
  id: string;
  ts: string;
  type: string;
  severity: Severity;
  title: string;
  message: string;
  read: boolean;
}

export interface Trade {
  id: string;
  tsym?: string;
  side?: string;
  instrument_token?: string;
  entry_ts: string;
  exit_ts: string;
  entry_price?: number;
  exit_price?: number;
  quantity?: number;
  lot_size?: number;
  lots?: number;
  contract_size?: number;
  pnl: string | number;
  gross_pnl?: number | null;
  fees_usd?: number | null;
  entry_fee_usd?: number | null;
  exit_fee_usd?: number | null;
  pnl_pct?: number | null;
  mfe?: number | null;
  mae?: number | null;
  exit_reason: string;
  setup_type: string;
  hold_seconds?: number | null;
  mode: string;
}

export interface TradesReportSummary {
  trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  total_pnl: number;
  avg_pnl: number;
  best_trade: number;
  worst_trade: number;
  gross_profit: number;
  gross_loss: number;
}

export interface TradesReport {
  from_date: string | null;
  to_date: string | null;
  generated_at: string;
  summary: TradesReportSummary;
  by_exit_reason: Record<string, { count: number; pnl: number }>;
  by_setup: Record<string, { count: number; pnl: number }>;
  by_day: Record<string, { count: number; pnl: number }>;
  trades: Trade[];
}

export interface WatchlistItem {
  token: string;
  tsym: string;
  strike: number;
  option_type: string;
  is_atm: boolean;
  tradable: boolean;
  lot_size?: number;
  contract_size?: number;
  symbol?: string;
  underlying?: string;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  volume: number | null;
  oi: number | null;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
  greeks_source?: string;
  last_update_ts: string | null;
}

export interface Watchlist {
  underlying: string;
  spot_ltp: number | null;
  atm_strike: number | null;
  expiry_symbol?: string | null;
  instrument_count: number;
  strike_count?: number;
  strike_band_points?: number;
  strike_step?: number;
  atm_strike_steps?: number;
  last_quote_ts: string | null;
  feed_mode?: string;
  market_open?: boolean;
  ws_open?: boolean;
  ws_quote_age_sec?: number | null;
  quote_age_sec?: number | null;
  greeks_source?: string;
  items: WatchlistItem[];
  open_positions?: WatchlistOpenPosition[];
  commodities?: CommoditySnapshot[];
  bias_5m?: string;
}

export interface WatchlistOpenPosition {
  position_id?: string;
  instrument_token?: string;
  tsym: string;
  side?: string;
  quantity: number;
  lot_size?: number;
  lots?: number;
  contract_size?: number;
  underlying_qty?: number;
  underlying?: string;
  entry_price: number;
  entry_ts?: string;
  current_ltp?: number;
  unrealized_pnl?: number;
  premium_deployed?: number;
  setup_type?: string;
  trail_floor?: number | null;
  mfe_inr?: number;
  last_tick_ts?: string | null;
  notional_usd?: number | null;
  premium_per_lot_usd?: number | null;
  entry_spot?: number | null;
  current_spot?: number | null;
  gross_unrealized_pnl?: number | null;
  net_unrealized_pnl?: number | null;
  entry_fee_usd?: number | null;
  estimated_exit_fee_usd?: number | null;
  fees_paid_usd?: number | null;
  fees_if_exit_now_usd?: number | null;
  entry_fee_detail?: {
    notional_usd?: number;
    premium_usd?: number;
    raw_fee_usd?: number;
    total_fee_usd?: number;
    gst_usd?: number;
    capped?: boolean;
    rate_pct?: number;
  };
}

export interface ClosedBlotterTrade {
  id: string;
  tsym: string;
  contract_size?: number;
  underlying_qty?: number;
  side?: string;
  entry_ts: string;
  exit_ts: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  lot_size: number;
  lots: number;
  pnl: number;
  gross_pnl?: number;
  fees_usd?: number;
  entry_fee_usd?: number;
  exit_fee_usd?: number;
  exit_reason: string;
  setup_type?: string;
  hold_seconds?: number;
}

export interface DecisionLogEvent {
  id: string;
  ts: string;
  event_type: string;
  severity: string;
  message: string;
  metadata: Record<string, unknown>;
}

export interface TradeBlotter {
  open_positions: WatchlistOpenPosition[];
  closed_trades: ClosedBlotterTrade[];
}

export interface AuthUser {
  username: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  username: string;
  expires_at: string;
}
