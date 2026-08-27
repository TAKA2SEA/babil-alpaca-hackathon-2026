"""
Stage E static security tests - Alpaca MCP read-only integration.

Source inspection (AST + config file read) of the Stage E MCP modules,
verifying mechanically:
  - the AI agent package contains no order-mutating call and never imports
    the execution engine or the Alpaca SDK,
  - the read-only allowlist and deny list are disjoint and exactly match
    the Stage E policy,
  - real Alpaca connections stay disabled until Stage F,
  - .mcp.json restricts ALPACA_TOOLSETS to the read-only toolsets only.

Same approach as test_static_security.py - pure source inspection, no
network, no credentials, no order API.
"""
import ast
import json
from pathlib import Path

from ai_agent import mcp_tool_client as mc
from ai_agent import real_mcp_transport as rmt

WORKSPACE = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache"}

# SDK mutating method names plus alpaca-mcp-server order/write tool names.
# The AI agent package must never call any of these.
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
        "do_not_exercise_options_position",
        "update_account_config",
    }
)

STAGE_E_ALLOWED_TOOLSETS = frozenset(
    {"account", "assets", "stock-data", "options-data", "news"}
)

STAGE_E_FORBIDDEN_TOKENS = frozenset(
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


def _ai_agent_py_files():
    root = WORKSPACE / "ai_agent"
    for p in sorted(root.rglob("*.py")):
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


def test_stage_e_allowlist_only_allowed_toolsets():
    assert mc.READ_ONLY_MCP_TOOLSETS == STAGE_E_ALLOWED_TOOLSETS
    assert set(mc.READ_ONLY_TOOLSETS_ENV_VALUE.split(",")) == STAGE_E_ALLOWED_TOOLSETS


def test_stage_e_forbidden_tokens_present_verbatim():
    assert STAGE_E_FORBIDDEN_TOKENS <= mc.FORBIDDEN_MCP_TOOLS


def test_allowlist_and_denylist_are_disjoint():
    assert mc.READ_ONLY_MCP_TOOL_NAMES.isdisjoint(mc.FORBIDDEN_MCP_TOOLS)


def test_every_allowlisted_tool_is_get_style():
    assert all(name.startswith("get_") for name in mc.READ_ONLY_MCP_TOOL_NAMES)


def test_ai_agent_has_no_order_mutating_calls():
    hits = []
    for p in _ai_agent_py_files():
        bad = _called_names(p) & FORBIDDEN_SDK_METHODS
        if bad:
            hits.append(f"{p.relative_to(WORKSPACE)}: {sorted(bad)}")
    assert not hits, "; ".join(hits)


def test_ai_agent_never_imports_execution_or_alpaca():
    hits = []
    for p in _ai_agent_py_files():
        bad = _imported_modules(p) & {"execution_engine", "alpaca"}
        if bad:
            hits.append(f"{p.relative_to(WORKSPACE)}: {sorted(bad)}")
    assert not hits, "; ".join(hits)


def test_real_connection_disabled_in_stage_e():
    assert rmt.REAL_CONNECTION_READY is False


def test_mcp_json_restricts_toolsets_to_read_only():
    mcp_json = WORKSPACE / ".mcp.json"
    assert mcp_json.exists(), "expected .mcp.json to define the read-only MCP server config"
    cfg = json.loads(mcp_json.read_text(encoding="utf-8"))
    servers = cfg.get("mcpServers", {})
    assert servers, ".mcp.json must declare at least one MCP server"
    for name, server in servers.items():
        env = server.get("env", {})
        toolsets_raw = env.get("ALPACA_TOOLSETS", "")
        toolsets = {t.strip() for t in toolsets_raw.split(",") if t.strip()}
        assert toolsets, f"{name}: ALPACA_TOOLSETS must be non-empty"
        assert toolsets <= mc.READ_ONLY_MCP_TOOLSETS, (
            f"{name}: ALPACA_TOOLSETS must stay within the read-only toolsets, got {sorted(toolsets)}"
        )
        assert "trading" not in toolsets
        paper_trade = str(env.get("ALPACA_PAPER_TRADE", "true")).strip().lower()
        assert paper_trade != "false", f"{name}: ALPACA_PAPER_TRADE must not disable paper mode"
