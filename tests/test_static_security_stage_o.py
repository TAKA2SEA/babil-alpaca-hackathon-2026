"""
Stage O+ static security - consolidated Full-Chain integration audit.

Focused, non-duplicative complement to the per-stage (H/I/J/K/L) static
tests. Proves for the WHOLE Stage H->L chain used by the O+ integration
test:

  - execution_engine / Alpaca / TradingClient / MCP / network import = 0
  - LIVE path = 0 and paper=False path = 0
  - the O+ integration test never invokes broker.submit directly and
    imports no network/Alpaca/MCP module
  - the chain's only broker boundary is the injected broker inside
    babil_paper_execution.submit_paper_execution (which is never called
    by the O+ ALLOW chain)

Pure source inspection - no network, no credentials, no order API.
"""
import ast
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent

CHAIN_MODULES = [
    WORKSPACE / "babil_authorization.py",
    WORKSPACE / "babil_authorization_consumer.py",
    WORKSPACE / "babil_human_approval.py",
    WORKSPACE / "babil_pre_execution.py",
    WORKSPACE / "babil_paper_execution.py",
]

O_PLUS_TEST = WORKSPACE / "tests" / "test_stage_o_full_chain.py"

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

ALLOWED_CHAIN_IMPORTS = frozenset(
    {
        "babil_authorization",
        "babil_authorization_consumer",
        "babil_human_approval",
        "babil_pre_execution",
        "copy",
        "datetime",
        "hashlib",
        "json",
        "uuid",
        "dataclasses",
        "enum",
        "typing",
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


def test_chain_modules_have_no_execution_or_network_imports():
    for path in CHAIN_MODULES:
        mods = _imported_modules(path)
        assert not (mods & FORBIDDEN_MODULES), f"{path.name}: {sorted(mods & FORBIDDEN_MODULES)}"
        assert not (mods & FORBIDDEN_NETWORK_AND_PROCESS_MODULES), (
            f"{path.name}: {sorted(mods & FORBIDDEN_NETWORK_AND_PROCESS_MODULES)}"
        )
        assert mods <= ALLOWED_CHAIN_IMPORTS, f"{path.name}: {sorted(mods - ALLOWED_CHAIN_IMPORTS)}"


def test_chain_modules_have_no_live_or_paper_false_path():
    for path in CHAIN_MODULES:
        text = path.read_text(encoding="utf-8")
        assert 'mode="LIVE"' not in text
        assert "paper=False" not in text


def test_o_plus_test_never_invokes_broker_submit_directly():
    text = O_PLUS_TEST.read_text(encoding="utf-8")
    assert "broker.submit(" not in text


def test_o_plus_test_imports_no_network_or_alpaca():
    mods = _imported_modules(O_PLUS_TEST)
    assert not (mods & FORBIDDEN_MODULES)
    assert not (mods & FORBIDDEN_NETWORK_AND_PROCESS_MODULES)
