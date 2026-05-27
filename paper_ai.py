#!/usr/bin/env python3
"""
Paper-trading crypto learner.

This is a research sandbox. It only simulates orders, stores every decision in
SQLite, and has no exchange key support. Do not treat its output as financial
advice or proof that a strategy is profitable.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.example.json"
DEFAULT_DB = APP_DIR / "paper_trader.sqlite3"
USER_AGENT = "crypto-paper-ai/0.1 paper-trading research"


@dataclass
class SymbolConfig:
    coin_id: str
    symbol: str
    market_symbol: str | None = None


@dataclass
class MarketSnapshot:
    coin_id: str
    symbol: str
    price: float
    ts: int
    quote_volume: float = 0.0
    price_change_pct_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0


@dataclass
class Decision:
    action: str
    confidence: float
    features: dict[str, float]
    reason: str


def utc_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def sigmoid(value: float) -> float:
    if value < -35:
        return 0.0
    if value > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def http_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class CoinGeckoFeed:
    def __init__(self, quote_currency: str) -> None:
        self.quote_currency = quote_currency.upper()
        self.vs_currency = "usd" if self.quote_currency == "USDT" else quote_currency.lower()
        self.market_data: dict[str, dict[str, Any]] = {}

    def load_markets(self, top_n: int) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode(
            {
                "vs_currency": self.vs_currency,
                "order": "volume_desc",
                "per_page": top_n,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            }
        )
        payload = http_json(f"https://api.coingecko.com/api/v3/coins/markets?{params}")
        self.market_data = {item.get("id", ""): item for item in payload}
        return payload

    def universe(self, config: dict[str, Any], store: "Store") -> list[SymbolConfig]:
        universe = config.get("universe", {})
        if universe.get("mode") != "coingecko_top_volume":
            return parse_symbols(config)

        top_n = int(universe.get("top_n", 50))
        excluded = set(universe.get("exclude_base_symbols", []))
        markets = self.load_markets(top_n + len(excluded) + 20)
        selected: list[SymbolConfig] = []
        for item in markets:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol or symbol in excluded:
                continue
            if "USD" in symbol:
                continue
            if safe_float(item.get("current_price")) > 1_000_000:
                continue
            selected.append(
                SymbolConfig(
                    coin_id=str(item.get("id", "")),
                    symbol=symbol,
                    market_symbol=symbol + self.quote_currency,
                )
            )
            if len(selected) >= top_n:
                break

        by_symbol = {item.symbol: item for item in selected}
        for open_symbol in store.open_symbols():
            by_symbol.setdefault(
                open_symbol,
                SymbolConfig(
                    coin_id=open_symbol.lower(),
                    symbol=open_symbol,
                    market_symbol=f"{open_symbol}{self.quote_currency}",
                ),
            )
        return list(by_symbol.values())

    def prices(self, symbols: list[SymbolConfig]) -> list[MarketSnapshot]:
        if not symbols:
            return []
        if self.market_data:
            now = utc_now()
            snapshots: list[MarketSnapshot] = []
            for item in symbols:
                market = self.market_data.get(item.coin_id)
                if not market:
                    continue
                price = safe_float(market.get("current_price"))
                if price <= 0:
                    continue
                snapshots.append(
                    MarketSnapshot(
                        coin_id=item.coin_id,
                        symbol=item.symbol.upper(),
                        price=price,
                        ts=now,
                        quote_volume=safe_float(market.get("total_volume")),
                        price_change_pct_24h=safe_float(
                            market.get("price_change_percentage_24h")
                        ),
                        high_24h=safe_float(market.get("high_24h")),
                        low_24h=safe_float(market.get("low_24h")),
                    )
                )
            return snapshots

        ids = ",".join(item.coin_id for item in symbols)
        params = urllib.parse.urlencode(
            {"ids": ids, "vs_currencies": self.vs_currency}
        )
        url = f"https://api.coingecko.com/api/v3/simple/price?{params}"
        payload = http_json(url)
        now = utc_now()
        snapshots: list[MarketSnapshot] = []
        for item in symbols:
            price = payload.get(item.coin_id, {}).get(self.vs_currency)
            if price is None:
                continue
            snapshots.append(
                MarketSnapshot(
                    coin_id=item.coin_id,
                    symbol=item.symbol.upper(),
                    price=float(price),
                    ts=now,
                )
            )
        return snapshots


class BinanceFeed:
    def __init__(self, quote_currency: str = "USDT") -> None:
        self.quote_currency = quote_currency.upper()
        self.ticker_24h: dict[str, dict[str, Any]] = {}

    def load_24h_tickers(self) -> list[dict[str, Any]]:
        payload = http_json("https://api.binance.com/api/v3/ticker/24hr")
        self.ticker_24h = {item.get("symbol", ""): item for item in payload}
        return payload

    def universe(self, config: dict[str, Any], store: "Store") -> list[SymbolConfig]:
        universe = config.get("universe", {})
        if universe.get("mode") != "binance_top_volume":
            return parse_symbols(config)

        top_n = int(universe.get("top_n", 50))
        excluded = set(universe.get("exclude_base_symbols", []))
        excluded_suffixes = tuple(universe.get("exclude_suffixes", []))
        payload = self.load_24h_tickers()
        candidates: list[tuple[float, SymbolConfig]] = []

        for item in payload:
            market_symbol = item.get("symbol", "")
            if not market_symbol.endswith(self.quote_currency):
                continue
            base_symbol = market_symbol[: -len(self.quote_currency)]
            if not base_symbol.isascii() or not base_symbol.isalnum():
                continue
            if base_symbol in excluded or base_symbol.endswith(excluded_suffixes):
                continue
            try:
                quote_volume = float(item.get("quoteVolume", 0.0))
                last_price = float(item.get("lastPrice", 0.0))
            except (TypeError, ValueError):
                continue
            if quote_volume <= 0 or last_price <= 0:
                continue
            candidates.append(
                (
                    quote_volume,
                    SymbolConfig(
                        coin_id=base_symbol.lower(),
                        symbol=base_symbol,
                        market_symbol=market_symbol,
                    ),
                )
            )

        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = [item[1] for item in candidates[:top_n]]
        by_symbol = {item.symbol: item for item in selected}
        for open_symbol in store.open_symbols():
            by_symbol.setdefault(
                open_symbol,
                SymbolConfig(
                    coin_id=open_symbol.lower(),
                    symbol=open_symbol,
                    market_symbol=f"{open_symbol}{self.quote_currency}",
                ),
            )
        return list(by_symbol.values())

    def prices(self, symbols: list[SymbolConfig]) -> list[MarketSnapshot]:
        if not symbols:
            return []
        if not self.ticker_24h:
            self.load_24h_tickers()
        market_symbols = [
            item.market_symbol or f"{item.symbol.upper()}USDT" for item in symbols
        ]
        url = "https://api.binance.com/api/v3/ticker/price"
        payload = http_json(url)
        allowed = set(market_symbols)
        prices = {
            item["symbol"]: float(item["price"])
            for item in payload
            if item.get("symbol") in allowed
        }
        now = utc_now()
        snapshots: list[MarketSnapshot] = []
        for item in symbols:
            market_symbol = item.market_symbol or f"{item.symbol.upper()}USDT"
            price = prices.get(market_symbol)
            if price is None:
                continue
            ticker = self.ticker_24h.get(market_symbol, {})
            snapshots.append(
                MarketSnapshot(
                    coin_id=item.coin_id,
                    symbol=item.symbol.upper(),
                    price=price,
                    ts=now,
                    quote_volume=safe_float(ticker.get("quoteVolume")),
                    price_change_pct_24h=safe_float(ticker.get("priceChangePercent")),
                    high_24h=safe_float(ticker.get("highPrice")),
                    low_24h=safe_float(ticker.get("lowPrice")),
                )
            )
        return snapshots


class Store:
    def __init__(self, path: Path, config: dict[str, Any]) -> None:
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.config = config
        self.setup()

    def setup(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                quote_volume REAL NOT NULL DEFAULT 0,
                price_change_pct_24h REAL NOT NULL DEFAULT 0,
                high_24h REAL NOT NULL DEFAULT 0,
                low_24h REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                quantity REAL NOT NULL DEFAULT 0,
                avg_entry REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cash (
                currency TEXT PRIMARY KEY,
                amount REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                cash_after REAL NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS models (
                symbol TEXT PRIMARY KEY,
                weights_json TEXT NOT NULL,
                last_feature_json TEXT,
                last_price REAL,
                updated_ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                requested_action TEXT NOT NULL,
                executed_action TEXT NOT NULL,
                price REAL NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                features_json TEXT NOT NULL,
                portfolio_value REAL NOT NULL,
                position_value REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decision_evaluations (
                decision_id INTEGER NOT NULL,
                horizon_seconds INTEGER NOT NULL,
                future_ts INTEGER NOT NULL,
                future_price REAL NOT NULL,
                benchmark_symbol TEXT NOT NULL,
                benchmark_return REAL NOT NULL,
                forward_return REAL NOT NULL,
                edge_return REAL NOT NULL,
                score REAL NOT NULL,
                label INTEGER NOT NULL,
                used_for_training INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(decision_id, horizon_seconds),
                FOREIGN KEY(decision_id) REFERENCES decisions(id)
            );
            """
        )
        self.migrate()
        quote = self.config.get("quote_currency", "USD").upper()
        existing = self.db.execute(
            "SELECT amount FROM cash WHERE currency = ?", (quote,)
        ).fetchone()
        if existing is None:
            self.db.execute(
                "INSERT INTO cash(currency, amount) VALUES(?, ?)",
                (quote, float(self.config.get("starting_cash", 10000.0))),
            )
        self.db.commit()

    def migrate(self) -> None:
        columns = {
            row["name"]
            for row in self.db.execute("PRAGMA table_info(prices)").fetchall()
        }
        migrations = {
            "quote_volume": "ALTER TABLE prices ADD COLUMN quote_volume REAL NOT NULL DEFAULT 0",
            "price_change_pct_24h": "ALTER TABLE prices ADD COLUMN price_change_pct_24h REAL NOT NULL DEFAULT 0",
            "high_24h": "ALTER TABLE prices ADD COLUMN high_24h REAL NOT NULL DEFAULT 0",
            "low_24h": "ALTER TABLE prices ADD COLUMN low_24h REAL NOT NULL DEFAULT 0",
        }
        for column, statement in migrations.items():
            if column not in columns:
                self.db.execute(statement)
        self.db.commit()

    def insert_price(self, snapshot: MarketSnapshot) -> None:
        self.db.execute(
            """
            INSERT INTO prices(
                ts, coin_id, symbol, price, quote_volume,
                price_change_pct_24h, high_24h, low_24h
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.ts,
                snapshot.coin_id,
                snapshot.symbol,
                snapshot.price,
                snapshot.quote_volume,
                snapshot.price_change_pct_24h,
                snapshot.high_24h,
                snapshot.low_24h,
            ),
        )
        self.db.commit()

    def recent_prices(self, symbol: str, limit: int = 80) -> list[float]:
        rows = self.db.execute(
            """
            SELECT price
            FROM prices
            WHERE symbol = ?
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()
        return [float(row["price"]) for row in reversed(rows)]

    def recent_market_rows(self, symbol: str, limit: int = 80) -> list[sqlite3.Row]:
        rows = self.db.execute(
            """
            SELECT price, quote_volume, price_change_pct_24h, high_24h, low_24h
            FROM prices
            WHERE symbol = ?
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()
        return list(reversed(rows))

    def cash(self) -> float:
        quote = self.config.get("quote_currency", "USD").upper()
        row = self.db.execute(
            "SELECT amount FROM cash WHERE currency = ?", (quote,)
        ).fetchone()
        return float(row["amount"]) if row else 0.0

    def set_cash(self, amount: float) -> None:
        quote = self.config.get("quote_currency", "USD").upper()
        self.db.execute(
            "UPDATE cash SET amount = ? WHERE currency = ?", (amount, quote)
        )

    def position(self, symbol: str) -> tuple[float, float]:
        row = self.db.execute(
            "SELECT quantity, avg_entry FROM positions WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            return 0.0, 0.0
        return float(row["quantity"]), float(row["avg_entry"])

    def set_position(self, symbol: str, quantity: float, avg_entry: float) -> None:
        self.db.execute(
            """
            INSERT INTO positions(symbol, quantity, avg_entry)
            VALUES(?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                quantity = excluded.quantity,
                avg_entry = excluded.avg_entry
            """,
            (symbol, quantity, avg_entry),
        )

    def model_state(self, symbol: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT weights_json, last_feature_json, last_price FROM models WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row is None:
            return {
                "weights": {
                    "bias": 0.0,
                    "ret_1": 0.0,
                    "ret_3": 0.0,
                    "ret_8": 0.0,
                    "ret_21": 0.0,
                    "vol_8": 0.0,
                    "rsi_14": 0.0,
                    "range_position_24h": 0.0,
                    "volume_change_5": 0.0,
                    "market_change_24h": 0.0,
                    "position": 0.0,
                },
                "last_features": None,
                "last_price": None,
            }
        return {
            "weights": json.loads(row["weights_json"]),
            "last_features": json.loads(row["last_feature_json"])
            if row["last_feature_json"]
            else None,
            "last_price": row["last_price"],
        }

    def save_model(
        self,
        symbol: str,
        weights: dict[str, float],
        features: dict[str, float],
        price: float,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO models(symbol, weights_json, last_feature_json, last_price, updated_ts)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                weights_json = excluded.weights_json,
                last_feature_json = excluded.last_feature_json,
                last_price = excluded.last_price,
                updated_ts = excluded.updated_ts
            """,
            (
                symbol,
                json.dumps(weights, sort_keys=True),
                json.dumps(features, sort_keys=True),
                price,
                utc_now(),
            ),
        )
        self.db.commit()

    def save_model_weights(self, symbol: str, weights: dict[str, float]) -> None:
        state = self.model_state(symbol)
        self.db.execute(
            """
            INSERT INTO models(symbol, weights_json, last_feature_json, last_price, updated_ts)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                weights_json = excluded.weights_json,
                updated_ts = excluded.updated_ts
            """,
            (
                symbol,
                json.dumps(weights, sort_keys=True),
                json.dumps(state["last_features"], sort_keys=True)
                if state["last_features"]
                else None,
                state["last_price"],
                utc_now(),
            ),
        )
        self.db.commit()

    def record_trade(
        self,
        symbol: str,
        action: str,
        quantity: float,
        price: float,
        confidence: float,
        reason: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO trades(ts, symbol, action, quantity, price, cash_after, confidence, reason)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (utc_now(), symbol, action, quantity, price, self.cash(), confidence, reason),
        )
        self.db.commit()

    def record_decision(
        self,
        symbol: str,
        requested_action: str,
        executed_action: str,
        price: float,
        confidence: float,
        reason: str,
        features: dict[str, float],
        portfolio_value: float,
        position_value: float,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO decisions(
                ts, symbol, requested_action, executed_action, price,
                confidence, reason, features_json, portfolio_value, position_value
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                symbol,
                requested_action,
                executed_action,
                price,
                confidence,
                reason,
                json.dumps(features, sort_keys=True),
                portfolio_value,
                position_value,
            ),
        )
        self.db.commit()

    def price_at_or_after(self, symbol: str, ts: int) -> sqlite3.Row | None:
        return self.db.execute(
            """
            SELECT ts, price
            FROM prices
            WHERE symbol = ? AND ts >= ?
            ORDER BY ts ASC, id ASC
            LIMIT 1
            """,
            (symbol, ts),
        ).fetchone()

    def evaluate_decisions(
        self,
        horizons: tuple[int, ...] = (300, 900, 3600),
        benchmark_symbol: str = "BTC",
    ) -> int:
        evaluated = 0
        decisions = self.db.execute(
            """
            SELECT id, ts, symbol, executed_action, price
            FROM decisions
            ORDER BY id
            """,
        ).fetchall()
        for decision in decisions:
            for horizon in horizons:
                exists = self.db.execute(
                    """
                    SELECT 1
                    FROM decision_evaluations
                    WHERE decision_id = ? AND horizon_seconds = ?
                    """,
                    (decision["id"], horizon),
                ).fetchone()
                if exists:
                    continue
                future = self.price_at_or_after(decision["symbol"], int(decision["ts"]) + horizon)
                if future is None:
                    continue
                benchmark_entry = self.price_at_or_after(benchmark_symbol, int(decision["ts"]))
                benchmark_future = self.price_at_or_after(
                    benchmark_symbol, int(decision["ts"]) + horizon
                )
                if benchmark_entry is None or benchmark_future is None:
                    benchmark_return = 0.0
                else:
                    benchmark_return = (
                        float(benchmark_future["price"]) / float(benchmark_entry["price"])
                    ) - 1.0
                entry_price = float(decision["price"])
                forward_return = (float(future["price"]) / entry_price) - 1.0 if entry_price > 0 else 0.0
                edge_return = forward_return - benchmark_return
                action = str(decision["executed_action"])
                if action == "BUY":
                    score = edge_return
                elif action == "SELL":
                    score = -edge_return
                else:
                    score = -abs(edge_return)
                label = 1 if edge_return > 0.001 else 0
                self.db.execute(
                    """
                    INSERT INTO decision_evaluations(
                        decision_id, horizon_seconds, future_ts, future_price,
                        benchmark_symbol, benchmark_return, forward_return,
                        edge_return, score, label
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision["id"],
                        horizon,
                        int(future["ts"]),
                        float(future["price"]),
                        benchmark_symbol,
                        benchmark_return,
                        forward_return,
                        edge_return,
                        score,
                        label,
                    ),
                )
                evaluated += 1
        self.db.commit()
        return evaluated

    def train_from_evaluations(self, horizon_seconds: int = 900) -> int:
        rows = self.db.execute(
            """
            SELECT e.decision_id, d.symbol, d.features_json, e.label
            FROM decision_evaluations e
            JOIN decisions d ON d.id = e.decision_id
            WHERE e.horizon_seconds = ? AND e.used_for_training = 0
            ORDER BY e.decision_id
            """,
            (horizon_seconds,),
        ).fetchall()
        learner = Learner(self.config)
        trained = 0
        for row in rows:
            symbol = str(row["symbol"])
            features = json.loads(row["features_json"])
            state = self.model_state(symbol)
            weights = learner.update_from_label(
                state["weights"],
                features,
                int(row["label"]),
            )
            self.save_model_weights(symbol, weights)
            self.db.execute(
                """
                UPDATE decision_evaluations
                SET used_for_training = 1
                WHERE decision_id = ? AND horizon_seconds = ?
                """,
                (row["decision_id"], horizon_seconds),
            )
            trained += 1
        self.db.commit()
        return trained

    def summary(self) -> dict[str, Any]:
        positions = self.db.execute(
            "SELECT symbol, quantity, avg_entry FROM positions ORDER BY symbol"
        ).fetchall()
        latest_prices = {
            row["symbol"]: float(row["price"])
            for row in self.db.execute(
                """
                SELECT p.symbol, p.price
                FROM prices p
                JOIN (
                    SELECT symbol, MAX(id) AS max_id
                    FROM prices
                    GROUP BY symbol
                ) latest ON latest.max_id = p.id
                """
            ).fetchall()
        }
        open_positions = []
        value = self.cash()
        for row in positions:
            quantity = float(row["quantity"])
            if quantity <= 0:
                continue
            price = latest_prices.get(row["symbol"], float(row["avg_entry"]))
            market_value = quantity * price
            value += market_value
            open_positions.append(
                {
                    "symbol": row["symbol"],
                    "quantity": quantity,
                    "avg_entry": float(row["avg_entry"]),
                    "last_price": price,
                    "market_value": market_value,
                    "pnl_pct": ((price / float(row["avg_entry"])) - 1.0)
                    if float(row["avg_entry"]) > 0
                    else 0.0,
                }
            )
        return {"cash": self.cash(), "portfolio_value": value, "positions": open_positions}

    def open_position_count(self) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS count FROM positions WHERE quantity > 0"
        ).fetchone()
        return int(row["count"]) if row else 0

    def open_symbols(self) -> list[str]:
        rows = self.db.execute(
            "SELECT symbol FROM positions WHERE quantity > 0 ORDER BY symbol"
        ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def report(self) -> dict[str, Any]:
        summary = self.summary()
        self.evaluate_decisions()
        trade_rows = self.db.execute(
            """
            SELECT ts, symbol, action, quantity, price, cash_after, confidence, reason
            FROM trades
            WHERE action IN ('BUY', 'SELL')
            ORDER BY id
            """
        ).fetchall()
        round_trips: list[dict[str, Any]] = []
        lots: dict[str, list[dict[str, float]]] = {}

        for row in trade_rows:
            symbol = str(row["symbol"])
            action = str(row["action"])
            quantity = float(row["quantity"])
            price = float(row["price"])
            if quantity <= 0:
                continue
            if action == "BUY":
                lots.setdefault(symbol, []).append(
                    {"quantity": quantity, "price": price, "ts": float(row["ts"])}
                )
                continue
            remaining = quantity
            symbol_lots = lots.setdefault(symbol, [])
            while remaining > 0 and symbol_lots:
                lot = symbol_lots[0]
                matched = min(remaining, lot["quantity"])
                pnl = (price - lot["price"]) * matched
                pnl_pct = (price / lot["price"]) - 1.0 if lot["price"] > 0 else 0.0
                round_trips.append(
                    {
                        "symbol": symbol,
                        "quantity": matched,
                        "entry": lot["price"],
                        "exit": price,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "entry_ts": int(lot["ts"]),
                        "exit_ts": int(row["ts"]),
                    }
                )
                lot["quantity"] -= matched
                remaining -= matched
                if lot["quantity"] <= 1e-12:
                    symbol_lots.pop(0)

        realized_pnl = sum(item["pnl"] for item in round_trips)
        winners = [item for item in round_trips if item["pnl"] > 0]
        losers = [item for item in round_trips if item["pnl"] < 0]
        best = max(round_trips, key=lambda item: item["pnl_pct"], default=None)
        worst = min(round_trips, key=lambda item: item["pnl_pct"], default=None)
        buys = sum(1 for row in trade_rows if row["action"] == "BUY")
        sells = sum(1 for row in trade_rows if row["action"] == "SELL")
        decision_count = int(
            self.db.execute("SELECT COUNT(*) AS count FROM decisions").fetchone()["count"]
        )
        evaluated_count = int(
            self.db.execute(
                "SELECT COUNT(*) AS count FROM decision_evaluations"
            ).fetchone()["count"]
        )
        trained_count = int(
            self.db.execute(
                "SELECT COUNT(*) AS count FROM decision_evaluations WHERE used_for_training = 1"
            ).fetchone()["count"]
        )
        outcome_rows = self.db.execute(
            """
            SELECT
                horizon_seconds,
                COUNT(*) AS count,
                AVG(edge_return) AS avg_edge_return,
                AVG(score) AS avg_score,
                AVG(label) AS positive_rate
            FROM decision_evaluations
            GROUP BY horizon_seconds
            ORDER BY horizon_seconds
            """
        ).fetchall()
        outcome_by_horizon = {
            str(row["horizon_seconds"]): {
                "count": int(row["count"]),
                "avg_edge_return": float(row["avg_edge_return"] or 0.0),
                "avg_score": float(row["avg_score"] or 0.0),
                "positive_rate": float(row["positive_rate"] or 0.0),
            }
            for row in outcome_rows
        }
        action_rows = self.db.execute(
            """
            SELECT
                d.executed_action,
                e.horizon_seconds,
                COUNT(*) AS count,
                AVG(e.edge_return) AS avg_edge_return,
                AVG(e.score) AS avg_score
            FROM decision_evaluations e
            JOIN decisions d ON d.id = e.decision_id
            GROUP BY d.executed_action, e.horizon_seconds
            ORDER BY d.executed_action, e.horizon_seconds
            """
        ).fetchall()
        action_outcomes = [
            {
                "action": row["executed_action"],
                "horizon_seconds": int(row["horizon_seconds"]),
                "count": int(row["count"]),
                "avg_edge_return": float(row["avg_edge_return"] or 0.0),
                "avg_score": float(row["avg_score"] or 0.0),
            }
            for row in action_rows
        ]

        return {
            "cash": summary["cash"],
            "portfolio_value": summary["portfolio_value"],
            "open_positions": summary["positions"],
            "decision_count": decision_count,
            "evaluated_decisions": evaluated_count,
            "trained_evaluations": trained_count,
            "outcomes_by_horizon_seconds": outcome_by_horizon,
            "action_outcomes": action_outcomes,
            "trade_count": len(trade_rows),
            "buy_count": buys,
            "sell_count": sells,
            "closed_positions": len(round_trips),
            "win_rate": (len(winners) / len(round_trips)) if round_trips else 0.0,
            "realized_pnl": realized_pnl,
            "avg_closed_pnl_pct": (
                sum(item["pnl_pct"] for item in round_trips) / len(round_trips)
                if round_trips
                else 0.0
            ),
            "best_closed_trade": best,
            "worst_closed_trade": worst,
        }


class Learner:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def features(
        self,
        rows: list[sqlite3.Row],
        position_value: float,
        portfolio: float,
    ) -> dict[str, float]:
        prices = [float(row["price"]) for row in rows]
        def ret(period: int) -> float:
            if len(prices) <= period or prices[-period - 1] <= 0:
                return 0.0
            return (prices[-1] / prices[-period - 1]) - 1.0

        def rsi(period: int = 14) -> float:
            if len(prices) <= period:
                return 0.0
            gains = []
            losses = []
            for i in range(len(prices) - period, len(prices)):
                change = prices[i] - prices[i - 1]
                if change >= 0:
                    gains.append(change)
                    losses.append(0.0)
                else:
                    gains.append(0.0)
                    losses.append(abs(change))
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            if avg_loss == 0:
                return 1.0
            value = 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
            return (value - 50.0) / 50.0

        def volume_change(period: int = 5) -> float:
            if len(rows) <= period:
                return 0.0
            current = float(rows[-1]["quote_volume"])
            previous = float(rows[-period - 1]["quote_volume"])
            if previous <= 0:
                return 0.0
            return max(-5.0, min(5.0, (current / previous) - 1.0))

        latest = rows[-1] if rows else None
        high_24h = float(latest["high_24h"]) if latest else 0.0
        low_24h = float(latest["low_24h"]) if latest else 0.0
        current_price = prices[-1] if prices else 0.0
        range_position = 0.0
        if high_24h > low_24h and current_price > 0:
            range_position = ((current_price - low_24h) / (high_24h - low_24h)) - 0.5

        returns = []
        for i in range(max(1, len(prices) - 8), len(prices)):
            if prices[i - 1] > 0:
                returns.append((prices[i] / prices[i - 1]) - 1.0)
        mean_return = sum(returns) / len(returns) if returns else 0.0
        variance = (
            sum((item - mean_return) ** 2 for item in returns) / len(returns)
            if returns
            else 0.0
        )
        return {
            "bias": 1.0,
            "ret_1": ret(1) * 100.0,
            "ret_3": ret(3) * 100.0,
            "ret_8": ret(8) * 100.0,
            "ret_21": ret(21) * 100.0,
            "vol_8": math.sqrt(variance) * 100.0,
            "rsi_14": rsi(14),
            "range_position_24h": range_position,
            "volume_change_5": volume_change(5),
            "market_change_24h": (float(latest["price_change_pct_24h"]) / 100.0)
            if latest
            else 0.0,
            "position": (position_value / portfolio) if portfolio > 0 else 0.0,
        }

    def update(
        self,
        weights: dict[str, float],
        last_features: dict[str, float] | None,
        last_price: float | None,
        current_price: float,
    ) -> dict[str, float]:
        if not last_features or not last_price or last_price <= 0:
            return weights
        target = 1.0 if current_price > last_price else 0.0
        prediction = sigmoid(sum(weights.get(k, 0.0) * v for k, v in last_features.items()))
        error = target - prediction
        learning_rate = float(self.config.get("learning_rate", 0.08))
        for key, value in last_features.items():
            weights[key] = weights.get(key, 0.0) + learning_rate * error * value
        return weights

    def update_from_label(
        self,
        weights: dict[str, float],
        features: dict[str, float],
        label: int,
    ) -> dict[str, float]:
        prediction = sigmoid(sum(weights.get(k, 0.0) * v for k, v in features.items()))
        error = float(label) - prediction
        learning_rate = float(self.config.get("learning_rate", 0.08))
        for key, value in features.items():
            weights[key] = weights.get(key, 0.0) + learning_rate * error * value
        return weights

    def decide(
        self,
        weights: dict[str, float],
        features: dict[str, float],
        avg_entry: float,
        price: float,
    ) -> Decision:
        score = sum(weights.get(key, 0.0) * value for key, value in features.items())
        up_probability = sigmoid(score)
        exploration = float(self.config.get("exploration_rate", 0.12))
        risk = self.config.get("risk", {})
        min_confidence = float(risk.get("min_confidence", 0.56))
        stop_loss_pct = float(risk.get("stop_loss_pct", 0.04))
        take_profit_pct = float(risk.get("take_profit_pct", 0.08))

        if random.random() < exploration:
            action = random.choice(["BUY", "SELL", "HOLD"])
            return Decision(action, up_probability, features, "exploration")

        if avg_entry > 0:
            change = (price / avg_entry) - 1.0
            if change <= -stop_loss_pct:
                return Decision("SELL", up_probability, features, "stop-loss")
            if change >= take_profit_pct:
                return Decision("SELL", up_probability, features, "take-profit")

        if up_probability >= min_confidence:
            return Decision("BUY", up_probability, features, "model expects upside")
        if up_probability <= (1.0 - min_confidence):
            return Decision("SELL", up_probability, features, "model expects downside")
        return Decision("HOLD", up_probability, features, "low confidence")


class PaperBroker:
    def __init__(self, store: Store, config: dict[str, Any]) -> None:
        self.store = store
        self.config = config

    def execute(self, snapshot: MarketSnapshot, decision: Decision) -> tuple[str, str]:
        cash = self.store.cash()
        quantity, avg_entry = self.store.position(snapshot.symbol)
        fee_rate = float(self.config.get("fee_rate", 0.001))
        slippage_rate = float(self.config.get("slippage_rate", 0.0005))
        position_fraction = float(self.config.get("position_fraction", 0.10))
        max_open_positions = int(self.config.get("max_open_positions", 2))

        summary = self.store.summary()
        portfolio_value = float(summary["portfolio_value"])
        position_value = quantity * snapshot.price
        target_position_value = portfolio_value * position_fraction

        if decision.action == "BUY":
            if quantity <= 0 and self.store.open_position_count() >= max_open_positions:
                self.store.record_trade(
                    snapshot.symbol,
                    "HOLD",
                    0.0,
                    snapshot.price,
                    decision.confidence,
                    "buy skipped: max open positions",
                )
                return "HOLD", "buy skipped: max open positions"
            budget = min(cash, max(0.0, target_position_value - position_value))
            if budget <= 1.0:
                self.store.record_trade(
                    snapshot.symbol,
                    "HOLD",
                    0.0,
                    snapshot.price,
                    decision.confidence,
                    "buy skipped: budget/risk cap",
                )
                return "HOLD", "buy skipped: budget/risk cap"
            fill_price = snapshot.price * (1.0 + slippage_rate)
            fee = budget * fee_rate
            bought = max(0.0, (budget - fee) / fill_price)
            new_quantity = quantity + bought
            new_avg = (
                ((quantity * avg_entry) + (bought * fill_price)) / new_quantity
                if new_quantity > 0
                else 0.0
            )
            self.store.set_cash(cash - budget)
            self.store.set_position(snapshot.symbol, new_quantity, new_avg)
            self.store.record_trade(
                snapshot.symbol, "BUY", bought, fill_price, decision.confidence, decision.reason
            )
            return "BUY", decision.reason

        if decision.action == "SELL":
            sell_quantity = quantity
            if sell_quantity <= 0:
                self.store.record_trade(
                    snapshot.symbol, "HOLD", 0.0, snapshot.price, decision.confidence, "sell skipped: no position"
                )
                return "HOLD", "sell skipped: no position"
            fill_price = snapshot.price * (1.0 - slippage_rate)
            proceeds = sell_quantity * fill_price
            fee = proceeds * fee_rate
            new_quantity = max(0.0, quantity - sell_quantity)
            self.store.set_cash(cash + proceeds - fee)
            self.store.set_position(
                snapshot.symbol,
                new_quantity,
                avg_entry if new_quantity > 0 else 0.0,
            )
            self.store.record_trade(
                snapshot.symbol, "SELL", sell_quantity, fill_price, decision.confidence, decision.reason
            )
            return "SELL", decision.reason

        self.store.record_trade(
            snapshot.symbol, "HOLD", 0.0, snapshot.price, decision.confidence, decision.reason
        )
        return "HOLD", decision.reason


def parse_symbols(config: dict[str, Any]) -> list[SymbolConfig]:
    return [
        SymbolConfig(
            coin_id=item["id"],
            symbol=item["symbol"],
            market_symbol=item.get("market_symbol"),
        )
        for item in config.get("symbols", [])
    ]


def run_once(store: Store, feed: Any, learner: Learner, broker: PaperBroker, config: dict[str, Any]) -> None:
    symbols = feed.universe(config, store) if hasattr(feed, "universe") else parse_symbols(config)
    snapshots = feed.prices(symbols)
    for snapshot in snapshots:
        store.insert_price(snapshot)
        quantity, avg_entry = store.position(snapshot.symbol)
        summary = store.summary()
        portfolio = float(summary["portfolio_value"])
        rows = store.recent_market_rows(snapshot.symbol)
        state = store.model_state(snapshot.symbol)
        weights = learner.update(
            state["weights"],
            state["last_features"],
            state["last_price"],
            snapshot.price,
        )
        feature_set = learner.features(rows, quantity * snapshot.price, portfolio)
        decision = learner.decide(weights, feature_set, avg_entry, snapshot.price)
        executed_action, executed_reason = broker.execute(snapshot, decision)
        store.record_decision(
            snapshot.symbol,
            decision.action,
            executed_action,
            snapshot.price,
            decision.confidence,
            executed_reason,
            feature_set,
            portfolio,
            quantity * snapshot.price,
        )
        store.save_model(snapshot.symbol, weights, feature_set, snapshot.price)
        print(
            f"{snapshot.symbol:>5} {snapshot.price:>12.4f} "
            f"{executed_action:<4} p_up={decision.confidence:.3f} {executed_reason}"
        )
    evaluated = store.evaluate_decisions()
    trained = store.train_from_evaluations()
    if evaluated or trained:
        print(f"learning_update evaluated={evaluated} trained={trained}")


def print_summary(store: Store) -> None:
    summary = store.summary()
    print(json.dumps(summary, indent=2, sort_keys=True))


def print_report(store: Store) -> None:
    store.evaluate_decisions()
    store.train_from_evaluations()
    print(json.dumps(store.report(), indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trading crypto learner")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--once", action="store_true", help="Run one market check and exit")
    parser.add_argument("--cycles", type=int, default=None, help="Run this many market checks and exit")
    parser.add_argument("--summary", action="store_true", help="Print portfolio summary and exit")
    parser.add_argument("--report", action="store_true", help="Print learning/trading report and exit")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    config = load_config(args.config)
    universe_mode = config.get("universe", {}).get("mode")
    if not parse_symbols(config) and universe_mode not in {
        "binance_top_volume",
        "coingecko_top_volume",
    }:
        raise SystemExit("No symbols configured.")

    store = Store(args.db, config)
    if args.report:
        print_report(store)
        return 0
    if args.summary:
        print_summary(store)
        return 0

    provider = config.get("market_data", {}).get("provider", "coingecko")
    feed = BinanceFeed(config.get("quote_currency", "USDT")) if provider == "binance" else CoinGeckoFeed(config.get("quote_currency", "USD"))
    learner = Learner(config)
    broker = PaperBroker(store, config)

    try:
        cycle = 0
        while True:
            run_once(store, feed, learner, broker, config)
            print_summary(store)
            cycle += 1
            if args.once or (args.cycles is not None and cycle >= args.cycles):
                return 0
            time.sleep(int(config.get("poll_seconds", 60)))
    except urllib.error.URLError as exc:
        raise SystemExit(f"Market data request failed: {exc}") from exc
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
