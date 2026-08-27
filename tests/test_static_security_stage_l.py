"""
Stage L static security tests - Paper Execution Adapter.

Source inspection (AST + source text) of babil_paper_execution.py, proving
mechanically:
  - execution_engine / Alpaca / TradingClient / MCP / network import = 0
  - the ONLY external execution dependency is the injected broker boundary
    (attribute calls on `broker` are limited to submit/verify_paper_account)
  - LIVE / cancel / replace / close_position / withdraw / transfer paths = 0
  - no execution-mode parameter, paper_only fixed True, no paper=False
  - execution kill switch defaults to OFF
  - ai_agent cannot reach this adapter directly (no AI -> Order shortcut)
"""
import ast
import inspect
from pathlib import Path

import babil_paper_execution as module

WORKSPACE = Path(__file__).resolve().parent.parent
STAGE_L_MODULE = WORKSPACE / "babil_paper_execution.py"

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

FORBIDDEN_DEFINITIONS = frozenset(
    {
        "submit_order",
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
        "mode=\"LIVE\"",
        "mode='LIVE'",
    }
)

ALLOWED_BROKER_CALLS = frozenset({"submit", "verify_paper_account"})


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


def _defined_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _broker_attribute_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "broker":
                calls.add(func.attr)
    return calls


def _ai_agent_imported_modules():
    names = set()
    for path in (WORKSPACE / "ai_agent").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    return names


def test_no_forbidden_imports():
    mods = _imported_modules(STAGE_L_MODULE)
    assert not (mods & FORBIDDEN_MODULES), f"forbidden imports: {sorted(mods & FORBIDDEN_MODULES)}"
    assert not (mods & FORBIDDEN_NETWORK_AND_PROCESS_MODULES), (
        f"network/process imports: {sorted(mods & FORBIDDEN_NETWORK_AND_PROCESS_MODULES)}"
    )
    assert mods <= {
        "babil_authorization", "babil_authorization_consumer", "babil_human_approval",
        "babil_pre_execution", "datetime", "uuid", "dataclasses", "enum", "typing",
    }, sorted(mods)


def test_no_forbidden_function_defined():
    defined = _defined_names(STAGE_L_MODULE)
    assert not (defined & FORBIDDEN_DEFINITIONS), f"forbidden definitions: {sorted(defined & FORBIDDEN_DEFINITIONS)}"


def test_no_live_or_forbidden_literal():
    text = STAGE_L_MODULE.read_text(encoding="utf-8")
    for token in sorted(FORBIDDEN_LITERALS):
        assert token not in text, f"forbidden literal {token!r} present in module"


def test_only_broker_boundary_external_calls():
    calls = _broker_attribute_calls(STAGE_L_MODULE)
    assert calls, "expected broker calls in the adapter"
    assert calls <= ALLOWED_BROKER_CALLS, f"unexpected broker calls: {sorted(calls - ALLOWED_BROKER_CALLS)}"


def test_execution_kill_switch_defaults_off():
    sig = inspect.signature(module.submit_paper_execution)
    assert sig.parameters["execution_enabled"].default is False


def test_no_execution_mode_parameter():
    for fn in ("build_paper_execution_request", "submit_paper_execution"):
        sig = inspect.signature(getattr(module, fn))
        assert "mode" not in sig.parameters
        assert "live" not in sig.parameters
        assert "paper" not in sig.parameters  # paper is fixed, never selectable


def test_paper_only_fixed_on_request_schema():
    import dataclasses

    names = {f.name for f in dataclasses.fields(module.PaperExecutionRequest)}
    assert "paper_only" in names
    for token in ("api_key", "secret", "token", "account_number", "client_order_id", "order_id"):
        assert token not in names


def test_ai_agent_cannot_reach_adapter_directly():
    ai_modules = _ai_agent_imported_modules()
    assert "babil_paper_execution" not in ai_modules
    assert "execution_engine" not in ai_modules
