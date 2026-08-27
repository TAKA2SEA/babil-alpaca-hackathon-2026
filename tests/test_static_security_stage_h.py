"""
Stage H static security tests - Execution Authorization Layer.

Source inspection (AST + source text) of babil_authorization.py, proving
mechanically that the authorization layer has no execution path:
  - imports no execution_engine, no Alpaca SDK, no trading/MCP transport,
  - calls no order-mutating SDK method,
  - defines no order-generating function,
  - contains no LIVE gate marker and no order-API literal.

Pure source inspection - no network, no credentials, no order API.
"""
import ast
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
STAGE_H_MODULE = WORKSPACE / "babil_authorization.py"

FORBIDDEN_MODULES = frozenset(
    {
        "execution_engine",
        "alpaca",
        "ai_agent",
        "mcp_tool_client",
        "real_mcp_transport",
        "competition_market_transport",
        "competition_account",
    }
)

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
        "build_order_request",
        "build_mleg_order_request",
    }
)

FORBIDDEN_ORDER_LITERALS = frozenset(
    {
        "submit_order",
        "place_order",
        "place_option_order",
        "cancel_order_by_id",
        "cancel_all_orders",
        "replace_order_by_id",
        "close_position",
        "close_all_positions",
        "build_order_request",
        "build_mleg_order_request",
    }
)


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


def _defined_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def test_stage_h_imports_no_execution_or_sdk_or_mcp():
    mods = _imported_modules(STAGE_H_MODULE)
    assert not (mods & FORBIDDEN_MODULES), f"forbidden imports: {sorted(mods & FORBIDDEN_MODULES)}"


def test_stage_h_calls_no_order_mutating_method():
    bad = _called_names(STAGE_H_MODULE) & FORBIDDEN_SDK_METHODS
    assert not bad, f"mutating call sites: {sorted(bad)}"


def test_stage_h_defines_no_order_generating_function():
    defined = _defined_names(STAGE_H_MODULE)
    bad = defined & FORBIDDEN_SDK_METHODS
    assert not bad, f"order-generating definitions: {sorted(bad)}"


def test_stage_h_has_no_live_gate_or_order_api_literal():
    text = STAGE_H_MODULE.read_text(encoding="utf-8")
    assert 'mode="LIVE"' not in text
    assert "mode='LIVE'" not in text
    for token in sorted(FORBIDDEN_ORDER_LITERALS):
        assert token not in text, f"forbidden literal {token!r} present in module"


def test_stage_h_authorization_record_is_not_an_order():
    import dataclasses

    import babil_authorization as auth

    names = {f.name for f in dataclasses.fields(auth.AuthorizationRecord)}
    assert "not_executable" in names
    for token in ("order_request", "order_id", "qty", "limit_price", "api_key", "secret", "token", "account_number"):
        assert token not in names
