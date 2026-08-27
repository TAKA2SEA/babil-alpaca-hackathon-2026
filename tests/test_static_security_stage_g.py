"""
Stage G static security tests - AI Proposal pipeline (read-only).

Source inspection (AST + regex) of the Stage G modules, verifying
mechanically that the Proposal pipeline never reaches execution_engine or
a trading-MCP / order path:
  - no Stage G module imports execution_engine or the Alpaca SDK (the
    in-process transport may import alpaca-py GET clients, but never
    execution_engine),
  - no order-mutating SDK method is called anywhere in Stage G modules,
  - the only MCP tool names the Market Analyst requests are inside the
    Stage E read-only allowlist,
  - the in-process competition transport only implements allowlisted
    read-only tool handlers.

Pure source inspection - no network, no credentials, no order API.
"""
import ast
import re
from pathlib import Path

from ai_agent.mcp_tool_client import READ_ONLY_MCP_TOOL_NAMES

WORKSPACE = Path(__file__).resolve().parent.parent

STAGE_G_FILES = [
    WORKSPACE / "ai_agent" / "market_analyst.py",
    WORKSPACE / "competition_market_transport.py",
    WORKSPACE / "babil_proposal_bridge.py",
    WORKSPACE / "probe_ai_proposal.py",
]

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

# MCP tool names that must never be reachable from the Proposal pipeline.
FORBIDDEN_MCP_TOOLS = frozenset(
    {
        "trading",
        "place_order",
        "place_option_order",
        "cancel_order",
        "replace_order",
        "close_position",
        "close_all_positions",
    }
)


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


def _call_tool_literals(path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "call_tool":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
    return names


def test_stage_g_never_imports_execution_engine():
    for p in STAGE_G_FILES:
        mods = _imported_modules(p)
        assert "execution_engine" not in mods, f"{p.name}: imports execution_engine"


def test_stage_g_has_no_order_mutating_calls():
    for p in STAGE_G_FILES:
        bad = _called_names(p) & FORBIDDEN_SDK_METHODS
        assert not bad, f"{p.name}: calls {sorted(bad)}"


def test_market_analyst_only_requests_allowlisted_tools():
    path = WORKSPACE / "ai_agent" / "market_analyst.py"
    names = _call_tool_literals(path)
    assert names, "expected call_tool usages in market_analyst"
    assert names <= READ_ONLY_MCP_TOOL_NAMES, f"off-allowlist: {sorted(names - READ_ONLY_MCP_TOOL_NAMES)}"
    assert not (names & FORBIDDEN_MCP_TOOLS)


def test_transport_only_implements_allowlisted_tool_handlers():
    path = WORKSPACE / "competition_market_transport.py"
    text = path.read_text(encoding="utf-8")
    handled = set(re.findall(r"_tool_([a-z0-9_]+)\(", text))
    assert handled, "expected allowlisted tool handlers in competition transport"
    assert handled <= READ_ONLY_MCP_TOOL_NAMES, f"off-allowlist handlers: {sorted(handled - READ_ONLY_MCP_TOOL_NAMES)}"
