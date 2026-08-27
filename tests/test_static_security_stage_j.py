"""
Stage J static security tests - Human Execution Authorization Boundary.

Source inspection (AST + source text) of babil_human_approval.py, proving
mechanically:
  - execution_engine / Alpaca / MCP / network import = 0
  - submit_order / place_order / cancel_order / replace_order /
    close_position call = 0
  - LIVE execution path = 0 (no LIVE gate marker, no mode field on the
    approval record)
  - Approval -> Order conversion function = 0
  - no automatic AI-output -> APPROVED path (no auto/from-ai function;
    approve() requires an explicit_confirmation parameter)

Pure source inspection - no network, no credentials, no order API.
"""
import ast
import dataclasses
from pathlib import Path

import babil_human_approval as approval_module

WORKSPACE = Path(__file__).resolve().parent.parent
STAGE_J_MODULE = WORKSPACE / "babil_human_approval.py"

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

FORBIDDEN_NETWORK_MODULES = frozenset(
    {"socket", "http", "urllib", "ssl", "requests", "websockets", "httpx", "asyncio"}
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
    {"execute", "trade", "to_order", "to_order_request", "auto_approve", "approve_from_ai"}
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
    mods = _imported_modules(STAGE_J_MODULE)
    assert not (mods & FORBIDDEN_MODULES), f"forbidden imports: {sorted(mods & FORBIDDEN_MODULES)}"
    assert not (mods & FORBIDDEN_NETWORK_MODULES), f"network imports: {sorted(mods & FORBIDDEN_NETWORK_MODULES)}"
    assert mods <= {"babil_authorization", "babil_authorization_consumer", "copy", "datetime", "uuid", "dataclasses", "enum", "typing"}, sorted(mods)


def test_no_order_mutating_calls():
    bad = _called_names(STAGE_J_MODULE) & FORBIDDEN_SDK_METHODS
    assert not bad, f"mutating call sites: {sorted(bad)}"


def test_no_execution_or_order_function_defined():
    defined = _defined_names(STAGE_J_MODULE)
    assert not (defined & FORBIDDEN_DEFINITIONS), f"forbidden definitions: {sorted(defined & FORBIDDEN_DEFINITIONS)}"


def test_no_live_gate_or_order_api_literal():
    text = STAGE_J_MODULE.read_text(encoding="utf-8")
    assert 'mode="LIVE"' not in text
    assert "mode='LIVE'" not in text
    for token in sorted(FORBIDDEN_LITERALS):
        assert token not in text, f"forbidden literal {token!r} present in module"


def test_approval_record_has_no_execution_mode_or_order_fields():
    names = {f.name for f in dataclasses.fields(approval_module.HumanApprovalRecord)}
    assert "not_executable" in names
    assert "mode" not in names  # approval is not an execution mode
    for token in ("order_id", "order_request", "qty", "price", "strike", "premium", "api_key", "secret", "token", "account_number"):
        assert token not in names


def test_approve_requires_explicit_confirmation():
    # structural: the only approval function that reaches APPROVED must take
    # an explicit_confirmation parameter (no automatic approval path exists)
    source = STAGE_J_MODULE.read_text(encoding="utf-8")
    assert "def approve(" in source
    assert "explicit_confirmation" in source
    # no function synthesizes APPROVED from an AI string
    for bad in ("def auto_approve", "def approve_from_ai", "explicit_confirmation = "):
        assert bad not in source, f"forbidden auto-approval construct {bad!r} present"
