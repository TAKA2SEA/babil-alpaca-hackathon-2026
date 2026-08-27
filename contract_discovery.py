"""
Phase 2 - Options contract discovery (GET-only).

Wraps alpaca-py's TradingClient.get_option_contracts() and
OptionHistoricalDataClient.get_option_latest_quote(). No mutating method
(submit_order, cancel_order_by_id, cancel_orders, exercise_options_position,
close_position, close_all_positions, replace_order_by_id) is called
anywhere in this file. TradingClient is always constructed with
paper=True - paper=False never appears in this workspace.
"""
import datetime as dt
import os

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest

from config import MARKET_DATA_BASE_URL, PAPER_KEY_ENV_VAR, PAPER_SECRET_ENV_VAR


class DiscoveryError(Exception):
    pass


def _read_paper_credentials():
    key = os.environ.get(PAPER_KEY_ENV_VAR)
    secret = os.environ.get(PAPER_SECRET_ENV_VAR)
    if not key or not secret:
        if os.path.exists(".env.paper"):
            with open(".env.paper") as f:
                for line in f:
                    if "=" in line and not line.startswith("#"):
                        k, v = line.strip().split("=", 1)
                        if k == PAPER_KEY_ENV_VAR:
                            key = v.strip("'\"")
                        if k == PAPER_SECRET_ENV_VAR:
                            secret = v.strip("'\"")
    if not key or not secret:
        raise DiscoveryError(f"{PAPER_KEY_ENV_VAR}/{PAPER_SECRET_ENV_VAR} not set")
    return key, secret


def make_trading_client():
    """Always paper=True. Never construct with paper=False in this workspace."""
    key, secret = _read_paper_credentials()
    return TradingClient(key, secret, paper=True)


def make_data_client():
    key, secret = _read_paper_credentials()
    # MARKET_DATA_BASE_URL is imported (not hardcoded here) purely so this
    # file also depends on config.py's single source of truth; the SDK
    # itself resolves the actual data host internally.
    assert MARKET_DATA_BASE_URL  # noqa: S101 - config already asserts the real value
    return OptionHistoricalDataClient(key, secret)


def field(obj, name, default=None):
    """Works whether `obj` is an SDK model instance, a dict, or a test double."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_str_value(value):
    """
    Real alpaca-py enum members (ContractType.CALL, AssetStatus.ACTIVE, ...)
    stringify via str() as "ContractType.CALL", not their value "call" -
    confirmed against the installed SDK. Prefer .value when present so
    comparisons against plain strings ("call") work for both real SDK
    objects and dict-based test doubles.
    """
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def fetch_active_contracts(
    underlying_symbol,
    trading_client=None,
    limit=100,
    contract_type=None,
    expiration_date_gte=None,
    expiration_date_lte=None,
):
    """
    GET-only: TradingClient.get_option_contracts(). Never places an order.

    expiration_date_gte/lte push the DTE window into the server-side query
    (the API returns contracts ordered by nearest expiration first, so
    without this a `limit` page can be exhausted by near-term expirations
    before ever reaching a later window). Callers should still apply
    filter_by_dte() locally afterward as a defensive double-check rather
    than trusting the server-side filter alone.
    """
    trading_client = trading_client or make_trading_client()
    kwargs = dict(underlying_symbols=[underlying_symbol], status="active", limit=limit)
    if contract_type is not None:
        kwargs["type"] = contract_type
    if expiration_date_gte is not None:
        kwargs["expiration_date_gte"] = expiration_date_gte
    if expiration_date_lte is not None:
        kwargs["expiration_date_lte"] = expiration_date_lte
    request = GetOptionContractsRequest(**kwargs)
    response = trading_client.get_option_contracts(request)
    contracts = field(response, "option_contracts", None)
    if contracts is None and isinstance(response, dict):
        contracts = response.get("option_contracts", [])
    return list(contracts or [])


def filter_by_type(contracts, option_type):
    option_type = str(option_type).lower()
    return [c for c in contracts if _as_str_value(field(c, "type", "")).lower() == option_type]


def filter_by_dte(contracts, min_days, max_days, today=None):
    """Returns a list of (contract, dte_days) tuples within [min_days, max_days]."""
    today = today or dt.date.today()
    out = []
    for c in contracts:
        exp = field(c, "expiration_date")
        if not exp:
            continue
        exp_date = exp if isinstance(exp, dt.date) else dt.date.fromisoformat(str(exp))
        dte = (exp_date - today).days
        if min_days <= dte <= max_days:
            out.append((c, dte))
    return out


def filter_by_strike_range(contracts, min_strike=None, max_strike=None):
    out = []
    for c in contracts:
        try:
            strike = float(field(c, "strike_price"))
        except (TypeError, ValueError):
            continue
        if min_strike is not None and strike < min_strike:
            continue
        if max_strike is not None and strike > max_strike:
            continue
        out.append(c)
    return out


def fetch_latest_quote(option_symbol, data_client=None):
    """GET-only: OptionHistoricalDataClient.get_option_latest_quote()."""
    data_client = data_client or make_data_client()
    request = OptionLatestQuoteRequest(symbol_or_symbols=option_symbol)
    quotes = data_client.get_option_latest_quote(request)
    if hasattr(quotes, "get"):
        return quotes.get(option_symbol)
    return quotes[option_symbol]


def make_stock_data_client():
    key, secret = _read_paper_credentials()
    return StockHistoricalDataClient(key, secret)


def fetch_underlying_spot_price(underlying_symbol, stock_data_client=None):
    """
    GET-only: StockHistoricalDataClient.get_stock_latest_trade(). Used only
    to compute ATM proximity for filter_by_atm_proximity() below - never
    used to size or price an order (risk_evaluator sizes off the option's
    own bid/ask, not the underlying).
    """
    stock_data_client = stock_data_client or make_stock_data_client()
    request = StockLatestTradeRequest(symbol_or_symbols=underlying_symbol)
    trades = stock_data_client.get_stock_latest_trade(request)
    trade = trades.get(underlying_symbol) if hasattr(trades, "get") else trades[underlying_symbol]
    price = field(trade, "price")
    if price is None:
        raise DiscoveryError(f"no trade price returned for {underlying_symbol}")
    return float(price)


def filter_by_atm_proximity(contracts, spot_price, pct=0.05):
    """
    Keeps contracts whose strike is within +/-pct of spot_price (default
    5%), sorted by proximity to spot (closest-to-the-money first). This is
    a pre-quote filter (uses only the strike already present on the
    contract) - it exists to avoid wasting quote GET calls on deep-ITM/OTM
    contracts far from a realistic ATM/near-OTM trading range.
    """
    spot_price = float(spot_price)
    lower = spot_price * (1 - pct)
    upper = spot_price * (1 + pct)
    in_range = []
    for c in contracts:
        try:
            strike = float(field(c, "strike_price"))
        except (TypeError, ValueError):
            continue
        if lower <= strike <= upper:
            in_range.append((abs(strike - spot_price), c))
    in_range.sort(key=lambda pair: pair[0])
    return [c for _distance, c in in_range]


def is_premium_in_range(bid, ask, min_premium=0.50, max_premium=10.00):
    """
    Post-quote check: True if the mid-price of (bid, ask) falls within
    [min_premium, max_premium] per share. Complements
    filter_by_atm_proximity() - strike proximity alone doesn't guarantee a
    tradeable premium band, since deep ITM contracts can be near-the-money
    in absolute strike terms but still carry very high premium.
    """
    try:
        bid_f, ask_f = float(bid), float(ask)
    except (TypeError, ValueError):
        return False
    if bid_f <= 0 or ask_f <= 0:
        return False
    mid = (bid_f + ask_f) / 2.0
    return min_premium <= mid <= max_premium
