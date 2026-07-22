# Algo-Crypto

Paper-first **BTC + Ethereum options** terminal on **Delta Exchange India**.

Independent from [algo-flat](../algo-flat) — separate Docker stack, Postgres, Redis, and ports.

## Stack

| Service | Host port | Notes |
|---------|-----------|--------|
| Web UI | **8081** | `admin` / `algocrypto` |
| Trading engine | **8002** | FastAPI health + control |
| Postgres | **5433** | DB `algocrypto` |
| Redis | **6380** | |

## Underlyings

- **BTC** and **ETH** options only (daily D1/D2 expiries, ~17:30 IST)
- Public REST works without API keys (paper); set `DELTA_API_KEY` / `DELTA_API_SECRET` for private endpoints and stronger WS auth
- Whitelist your machine/VPS IP on the Delta API key

## Quick start

```bash
cd ~/Desktop/algo-crypto   # or your clone path
cp .env.example .env
# optional: fill DELTA_API_KEY / DELTA_API_SECRET

docker compose up -d --build
```

Open http://localhost:8081

## Config

- `config/broker_config.yaml` — Delta India REST/WS
- `config/symbols_config.yaml` — BTC + ETH, index symbols, ATM band
- `config/market_session_config.yaml` — 24×7 + expiry cutoff

## Layout

Same UX as Algo-Flat (terminal, holdings, P&L, order book, decision logs) with an amber/orange theme so the two apps are visually distinct.
