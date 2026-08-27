"""
Stage F unit tests - Competition Paper Account isolation and READ-ONLY
validation.

Mechanically verifies that the Competition credential namespace
(ALPACA_COMPETITION_KEY_ID / ALPACA_COMPETITION_SECRET_KEY) is fully
isolated from the Dev paper namespace (config.py), that no live
credential name or credential value leaks into source/config, that
.env.competition is git-ignored, that .mcp.json carries no credential
values, and that the Competition account path (competition_account.py,
probe_competition_account.py) can never reach execution_engine.py or any
trading MCP tool - the only SDK call it makes is get_account() (GET).

No network, no real credentials, no order API.
"""
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import competition_account as ca
import config

from test_static_security import ALLOWED_CRED_ENV_VARS, LIVE_CRED_ENV_VARS

WORKSPACE = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache"}

COMPETITION_PATH_FILES = [
    WORKSPACE / "competition_account.py",
    WORKSPACE / "probe_competition_account.py",
]

# SDK mutating method names (and trading-MCP order tools). None may be
# called anywhere on the Competition account path.
FORBIDDEN_SDK_METHODS = frozenset(
    {
        "submit_order",
        "cancel_order_by_id",
        "cancel_orders",
        "exercise_options_position",
        "close_position",
        "close_all_positions",
        "replace_order_by_id",
        "place_stock_order",
        "place_crypto_order",
        "place_order",
        "place_option_order",
        "cancel_order",
        "replace_order",
        "cancel_all_orders",
    }
)

# Assignment of a non-empty string value to a credential env var name.
CRED_VALUE_ASSIGN_RE = re.compile(
    r"(?:ALPACA_PAPER_KEY_ID|ALPACA_PAPER_SECRET_KEY|"
    r"ALPACA_COMPETITION_KEY_ID|ALPACA_COMPETITION_SECRET_KEY)\s*=\s*[\"'](?P<val>[^\"']+)"
)

COMPETITION_KEY = "ALPACA_COMPETITION_KEY_ID"
COMPETITION_SECRET = "ALPACA_COMPETITION_SECRET_KEY"


def _workspace_py_files():
    for p in sorted(WORKSPACE.rglob("*.py")):
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        yield p


def _called_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# namespace isolation
# ---------------------------------------------------------------------------


def test_competition_names_differ_from_dev_names():
    dev = {config.PAPER_KEY_ENV_VAR, config.PAPER_SECRET_ENV_VAR}
    comp = {ca.COMPETITION_KEY_ID_ENV, ca.COMPETITION_SECRET_KEY_ENV}
    assert comp.isdisjoint(dev)


def test_competition_names_are_on_the_credential_allowlist():
    assert COMPETITION_KEY in ALLOWED_CRED_ENV_VARS
    assert COMPETITION_SECRET in ALLOWED_CRED_ENV_VARS


def test_competition_names_are_not_live_credential_names():
    assert COMPETITION_KEY not in LIVE_CRED_ENV_VARS
    assert COMPETITION_SECRET not in LIVE_CRED_ENV_VARS


def test_competition_credentials_configured_false_without_env(monkeypatch):
    monkeypatch.delenv(COMPETITION_KEY, raising=False)
    monkeypatch.delenv(COMPETITION_SECRET, raising=False)
    assert ca.competition_credentials_configured() is False


def test_competition_credentials_configured_true_with_env(monkeypatch):
    monkeypatch.setenv(COMPETITION_KEY, "k")
    monkeypatch.setenv(COMPETITION_SECRET, "s")
    assert ca.competition_credentials_configured() is True


def test_competition_client_uses_only_competition_creds_even_if_dev_set(monkeypatch):
    monkeypatch.setenv(COMPETITION_KEY, "comp-key")
    monkeypatch.setenv(COMPETITION_SECRET, "comp-secret")
    monkeypatch.setenv(config.PAPER_KEY_ENV_VAR, "dev-key")
    monkeypatch.setenv(config.PAPER_SECRET_ENV_VAR, "dev-secret")
    fake_tc = mock.Mock()
    monkeypatch.setattr(ca, "TradingClient", fake_tc)
    ca.make_competition_trading_client()
    fake_tc.assert_called_once_with("comp-key", "comp-secret", paper=True)


def test_competition_client_fails_closed_without_competition_creds(monkeypatch):
    monkeypatch.delenv(COMPETITION_KEY, raising=False)
    monkeypatch.delenv(COMPETITION_SECRET, raising=False)
    monkeypatch.setenv(config.PAPER_KEY_ENV_VAR, "dev-key")
    monkeypatch.setenv(config.PAPER_SECRET_ENV_VAR, "dev-secret")
    fake_tc = mock.Mock()
    monkeypatch.setattr(ca, "TradingClient", fake_tc)
    with pytest.raises(ca.CompetitionAccountError):
        ca.make_competition_trading_client()
    fake_tc.assert_not_called()


def test_competition_client_always_uses_paper_true():
    text = (WORKSPACE / "competition_account.py").read_text(encoding="utf-8")
    assert "paper=True" in text
    assert "paper=False" not in text


# ---------------------------------------------------------------------------
# no credential values in source / config
# ---------------------------------------------------------------------------


def test_no_credential_values_assigned_in_source():
    hits = []
    for p in _workspace_py_files():
        for lineno, line in enumerate(
            p.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            if CRED_VALUE_ASSIGN_RE.search(line):
                hits.append(f"{p.relative_to(WORKSPACE)}:{lineno}")
    assert not hits, "; ".join(hits)


def test_env_competition_is_gitignored():
    gitignore_lines = (WORKSPACE / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env.competition" in gitignore_lines


def test_mcp_json_has_no_credential_values():
    cfg = json.loads((WORKSPACE / ".mcp.json").read_text(encoding="utf-8"))
    for name, server in cfg.get("mcpServers", {}).items():
        env = server.get("env", {})
        for key, value in env.items():
            if re.search(r"KEY|SECRET", key, re.IGNORECASE):
                assert not str(value).strip(), f"{name}: {key} must not carry a value"


# ---------------------------------------------------------------------------
# competition path is READ-ONLY and never reaches execution / trading MCP
# ---------------------------------------------------------------------------


def test_competition_path_has_no_mutating_calls():
    for p in COMPETITION_PATH_FILES:
        bad = _called_names(p) & FORBIDDEN_SDK_METHODS
        assert not bad, f"{p.name}: calls {sorted(bad)}"


def test_competition_path_never_imports_execution_or_mcp():
    for p in COMPETITION_PATH_FILES:
        mods = _imported_modules(p)
        assert not (mods & {"execution_engine", "ai_agent"}), (
            f"{p.name}: imports {sorted(mods & {'execution_engine', 'ai_agent'})}"
        )


def test_verify_competition_account_is_get_only_and_masked(monkeypatch):
    account = SimpleNamespace(
        id="A00000000000000001",
        account_number="1234567890",
        status="ACTIVE",
        currency="USD",
        trading_blocked=False,
        account_blocked=False,
        pattern_day_trader=False,
        shorting_enabled=True,
        options_trading_level=3,
        options_approved_level=3,
    )

    class StrictFakeClient:
        def get_account(self):
            return account

        def __getattr__(self, name):
            raise AssertionError(f"method {name!r} must never be called on the Competition path")

    monkeypatch.setattr(ca, "make_competition_trading_client", lambda: StrictFakeClient())
    info = ca.verify_competition_account()

    assert info["status"] == "ACTIVE"
    assert info["currency"] == "USD"
    assert info["options_trading_level"] == 3
    assert info["verified_at"]
    assert "0000000001" not in info["account_number_masked"]
    assert "*" in info["account_number_masked"]
    assert COMPETITION_KEY not in str(info)
    assert COMPETITION_SECRET not in str(info)


def test_verify_competition_account_fails_closed_without_creds(monkeypatch):
    monkeypatch.delenv(COMPETITION_KEY, raising=False)
    monkeypatch.delenv(COMPETITION_SECRET, raising=False)
    with pytest.raises(ca.CompetitionAccountError):
        ca.verify_competition_account()
