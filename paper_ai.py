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


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def http_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class CoinGeckoFeed:
    def __init__(self, quote_currency: str) -> None:
        self.quote_currency = quote_currency.lower()

    def prices(self, symbols: list[SymbolConfig]) -> list[MarketSnapshot]:
        ids = ",".join(item.coin_id for item in symbols)
        params = urllib.parse.urlencode(
            {"ids": ids, "vs_currencies": self.quote_currency}
        )
        url = f"https://api.coingecko.com/api/v3/simple/price?{params}"
        payload = http_json(url)
        now = utc_now()
        snapshots: list[MarketSnapshot] = []
        for item in symbols:
            price = payload.get(item.coin_id, {}).get(self.quote_currency)
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

    def universe(self, config: dict[str, Any], store: "Store") -> list[SymbolConfig]:
        universe = config.get("universe", {})
        if universe.get("mode") != "binance_top_volume":
            return parse_symbols(config)

        top_n = int(universe.get("top_n", 50))
        excluded = set(universe.get("exclude_base_symbols", []))
        excluded_suffixes = tuple(universe.get("exclude_suffixes", []))
        url = "https://api.binance.com/api/v3/ticker/24hr"
        payload = http_json(url)
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
            snapshots.append(
                MarketSnapshot(
                    coin_id=item.coin_id,
                    symbol=item.symbol.upper(),
                    price=price,
                    ts=now,
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
                price REAL NOT NULL
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
            """
        )
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

    def insert_price(self, snapshot: MarketSnapshot) -> None:
        self.db.execute(
            "INSERT INTO prices(ts, coin_id, symbol, price) VALUES(?, ?, ?, ?)",
            (snapshot.ts, snapshot.coin_id, snapshot.symbol, snapshot.price),
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
                    "vol_8": 0.0,
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


class Learner:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def features(self, prices: list[float], position_value: float, portfolio: float) -> dict[str, float]:
        def ret(period: int) -> float:
            if len(prices) <= period or prices[-period - 1] <= 0:
                return 0.0
            return (prices[-1] / prices[-period - 1]) - 1.0

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
            "vol_8": math.sqrt(variance) * 100.0,
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
        prices = store.recent_prices(snapshot.symbol)
        state = store.model_state(snapshot.symbol)
        weights = learner.update(
            state["weights"],
            state["last_features"],
            state["last_price"],
            snapshot.price,
        )
        feature_set = learner.features(prices, quantity * snapshot.price, portfolio)
        decision = learner.decide(weights, feature_set, avg_entry, snapshot.price)
        executed_action, executed_reason = broker.execute(snapshot, decision)
        store.save_model(snapshot.symbol, weights, feature_set, snapshot.price)
        print(
            f"{snapshot.symbol:>5} {snapshot.price:>12.4f} "
            f"{executed_action:<4} p_up={decision.confidence:.3f} {executed_reason}"
        )


def print_summary(store: Store) -> None:
    summary = store.summary()
    print(json.dumps(summary, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trading crypto learner")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--once", action="store_true", help="Run one market check and exit")
    parser.add_argument("--cycles", type=int, default=None, help="Run this many market checks and exit")
    parser.add_argument("--summary", action="store_true", help="Print portfolio summary and exit")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    config = load_config(args.config)
    if not parse_symbols(config) and config.get("universe", {}).get("mode") != "binance_top_volume":
        raise SystemExit("No symbols configured.")

    store = Store(args.db, config)
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
