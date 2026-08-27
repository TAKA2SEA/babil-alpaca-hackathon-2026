"""
Dynamic Proposal Discovery - static security audit.

Source inspection (AST + source text) of dynamic_proposal_discovery.py:
  - execution_engine / Alpaca / MCP / network import = 0
  - order-mutating API call = 0 (and no function that could submit)
  - broker.submit invocation = 0
  - LIVE / paper=False path = 0
  - it only reads real data through injected provider + existing pure gates
"""
import ast
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
MODULE = WORKSPACE / "dynamic_proposal_discovery.py"

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
        "babil_paper_execution",
        "requests",
        "httpx",
        "socket",
        "urllib",
        "http",
        "ssl",
        "asyncio",
        "subprocess",
    }
)

FORBIDDEN_DEFINITIONS = frozenset(
    {
        "submit_order",
        "submit",
        "place_order",
        "place_option_order",
        "cancel_order",
        "cancel_order_by_id",
        "replace_order",
        "replace_order_by_id",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
        "withdraw",
        "transfer",
        "execute",
        "to_order",
        "to_order_request",
    }
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
        "paper=False",
        'mode="LIVE"',
        "mode='LIVE'",
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
    mods = _imported_modules(MODULE)
    assert not (mods & FORBIDDEN_MODULES), f"forbidden imports: {sorted(mods & FORBIDDEN_MODULES)}"


def test_no_order_mutating_calls():
    bad = _called_names(MODULE) & FORBIDDEN_DEFINITIONS
    assert not bad, f"forbidden call sites: {sorted(bad)}"


def test_no_submit_or_order_function_defined():
    defined = _defined_names(MODULE)
    assert not (defined & FORBIDDEN_DEFINITIONS), f"forbidden definitions: {sorted(defined & FORBIDDEN_DEFINITIONS)}"


def test_no_broker_invocation():
    text = MODULE.read_text(encoding="utf-8")
    assert "broker." not in text


def test_no_live_or_forbidden_literal():
    text = MODULE.read_text(encoding="utf-8")
    for token in sorted(FORBIDDEN_LITERALS):
        assert token not in text, f"forbidden literal {token!r} present in module"


def test_uses_only_existing_gates_and_provider():
    text = MODULE.read_text(encoding="utf-8")
    # it must consume the unchanged gate evaluator and an injected quote provider
    assert "evaluate_all_gates" in text
    assert "quote_provider" in text
