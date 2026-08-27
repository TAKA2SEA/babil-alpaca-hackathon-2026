"""
Stage K static security tests - Pre-Execution Boundary / Execution Firewall.

Source inspection (AST + source text) of babil_pre_execution.py, proving
mechanically:
  - execution_engine / Alpaca / TradingClient / MCP / network import = 0
  - mutating API call = 0
  - LIVE path = 0 (no LIVE gate, no paper=False path)
  - order creation function = 0, PreExecutionRecord -> Order conversion = 0
  - paper_only / not_executed are fixed True on the schema

Pure source inspection - no network, no credentials, no order API.
"""
import ast
import dataclasses
from pathlib import Path

import babil_pre_execution as module

WORKSPACE = Path(__file__).resolve().parent.parent
STAGE_K_MODULE = WORKSPACE / "babil_pre_execution.py"

FORBIDDEN_MODULES = frozenset(
    {
        "execution_engine",
        "alpaca",
        "ai_agent",
        "mcp_tool_client",
        "real_mcp_transport",
        "competition_market_transport",
        "competition_account",
        "babil_alpaca_orchestrator",
        "babil_proposal_bridge",
        "contract_discovery",
        "risk_evaluator",
        "mleg_builder",
    }
)

FORBIDDEN_NETWORK_AND_PROCESS_MODULES = frozenset(
    {"socket", "http", "urllib", "ssl", "requests", "websockets", "httpx", "asyncio", "subprocess"}
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

FORBIDDEN_DEFINITIONS = FORBIDDEN_SDK_METHODS | frozenset(
    {"execute", "trade", "to_order", "to_order_request"}
)

FORBIDDEN_LITERALS = frozenset(
    {
        "submit_order",
        "place_order",
        "place_option_order",
        "cancel_order_by_id",
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


def test_no_forbidden_imports():
    mods = _imported_modules(STAGE_K_MODULE)
    assert not (mods & FORBIDDEN_MODULES), f"forbidden imports: {sorted(mods & FORBIDDEN_MODULES)}"
    assert not (mods & FORBIDDEN_NETWORK_AND_PROCESS_MODULES), (
        f"network/process imports: {sorted(mods & FORBIDDEN_NETWORK_AND_PROCESS_MODULES)}"
    )
    assert mods <= {
        "babil_authorization", "babil_authorization_consumer", "babil_human_approval",
        "datetime", "uuid", "dataclasses", "enum", "typing",
    }, sorted(mods)


def test_no_order_mutating_calls():
    bad = _called_names(STAGE_K_MODULE) & FORBIDDEN_SDK_METHODS
    assert not bad, f"mutating call sites: {sorted(bad)}"


def test_no_execution_or_order_function_defined():
    defined = _defined_names(STAGE_K_MODULE)
    assert not (defined & FORBIDDEN_DEFINITIONS), f"forbidden definitions: {sorted(defined & FORBIDDEN_DEFINITIONS)}"


def test_no_live_or_paper_false_path():
    text = STAGE_K_MODULE.read_text(encoding="utf-8")
    assert 'mode="LIVE"' not in text
    assert "mode='LIVE'" not in text
    assert "paper=False" not in text
    assert "paper=False" not in text
    for token in sorted(FORBIDDEN_LITERALS):
        assert token not in text, f"forbidden literal {token!r} present in module"


def test_record_schema_is_fixed_paper_only_not_executed():
    names = {f.name for f in dataclasses.fields(module.PreExecutionRecord)}
    assert "paper_only" in names and "not_executed" in names and "execution_ready" in names
    for token in ("order_id", "order_request", "qty", "price", "strike", "premium", "api_key", "secret", "token", "account_number", "position", "execution_result"):
        assert token not in names


def test_no_order_generation_path_from_ready_record():
    # there must be no callable that takes a PreExecutionRecord and yields an order
    for name in ("to_order", "to_order_request", "build_order_request", "execute"):
        assert not hasattr(module, name)
