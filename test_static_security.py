#!/usr/bin/env python3
"""
Static security tests - Alpaca Hackathon 2026 (Paper-only workspace).

Pure source-text inspection of every .py file in this workspace. This file
itself makes zero network calls, reads zero credentials, and calls no
order-mutating method - it only reads other files as text and pattern
matches against them.

Run: python3 test_static_security.py
"""
import re
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
EXCLUDE_DIRS = {".git", ".venv", "__pycache__"}

# Matches "api.alpaca.markets" unless immediately preceded by "paper-",
# so it catches "https://api.alpaca.markets" but not
# "https://paper-api.alpaca.markets".
LIVE_ENDPOINT_RE = re.compile(r"(?<!paper-)api\.alpaca\.markets")

LIVE_CRED_FILES = [
    "/root/.alpaca_env",
    "/root/.alpaca_keys.env",
    "/root/vix_swing_bot/.env",
]

LIVE_CRED_ENV_VARS = [
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
]

# Stage F: the Competition paper-account credential namespace is added to
# the SAME positive allowlist so this mechanical check recognises the new
# sanctioned names instead of flagging them. Values are still never read
# or logged anywhere.
ALLOWED_CRED_ENV_VARS = {
    "ALPACA_PAPER_KEY_ID",
    "ALPACA_PAPER_SECRET_KEY",
    "ALPACA_COMPETITION_KEY_ID",
    "ALPACA_COMPETITION_SECRET_KEY",
}

# Any token shaped like a real Alpaca-style credential env var name (ends
# in _KEY_ID or _SECRET_KEY, matching APCA_API_KEY_ID / ALPACA_API_SECRET_KEY
# / ALPACA_PAPER_KEY_ID etc.) must be on the allowlist below. This is a
# positive check - it catches unexpected new credential names too, not just
# the four known-bad ones. It deliberately does NOT match every identifier
# that merely contains the substring KEY or SECRET (e.g. a local variable
# like PAPER_KEY or a helper constant like PAPER_KEY_ENV_VAR), since those
# are not credential *names* being referenced.
CRED_NAME_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:KEY_ID|SECRET_KEY)\b")

PAPER_ENDPOINT_LITERAL = "https://paper-api.alpaca.markets"
FORBIDDEN_PATH_REFS = ["vix_swing_bot"]
FORBIDDEN_QUARANTINE_REFS = ["_quarantine_alpaca_proto", "alpaca_execution_engine_v0_1"]

# Order-mutating SDK methods. Banned everywhere in the workspace EXCEPT
# execution_engine.py, which is the one designated execution module (see
# test_10 below, which additionally verifies its submit_order call site is
# textually gated behind a mode != "LIVE" early return).
MUTATING_METHOD_PATTERNS = [
    "submit_order(",
    "cancel_order_by_id(",
    "cancel_orders(",
    "exercise_options_position(",
    "close_position(",
    "close_all_positions(",
    "replace_order_by_id(",
]
MUTATING_METHODS_ALLOWED_FILE = "execution_engine.py"

RESULTS = []


def _py_files():
    self_path = Path(__file__).resolve()
    for p in sorted(WORKSPACE.rglob("*.py")):
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.resolve() == self_path:
            continue
        yield p


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail and not ok:
        line += f" - {detail}"
    print(line)


def test_1_no_live_endpoint():
    hits = []
    for p in _py_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in LIVE_ENDPOINT_RE.finditer(text):
            hits.append(f"{p.relative_to(WORKSPACE)}: {m.group(0)!r}")
    check("1_no_live_endpoint_reference", len(hits) == 0, "; ".join(hits))


def test_2_no_live_credential_file_paths():
    hits = []
    for p in _py_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in LIVE_CRED_FILES:
            if bad in text:
                hits.append(f"{p.relative_to(WORKSPACE)}: {bad!r}")
    check("2_no_live_credential_file_reference", len(hits) == 0, "; ".join(hits))


def test_3_no_live_credential_env_var_names():
    hits = []
    for p in _py_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in LIVE_CRED_ENV_VARS:
            if re.search(rf"\b{bad}\b", text):
                hits.append(f"{p.relative_to(WORKSPACE)}: {bad!r}")
    check("3_no_live_credential_env_var_reference", len(hits) == 0, "; ".join(hits))


def test_4_only_allowlisted_credential_names():
    hits = []
    for p in _py_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in CRED_NAME_RE.finditer(text):
            name = m.group(0)
            if name not in ALLOWED_CRED_ENV_VARS:
                hits.append(f"{p.relative_to(WORKSPACE)}: {name!r}")
    check("4_only_allowlisted_credential_names_used", len(hits) == 0, "; ".join(hits))


def test_5_6_paper_endpoint_single_source_of_truth():
    occurrences = []
    for p in _py_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        if PAPER_ENDPOINT_LITERAL in text:
            occurrences.append(str(p.relative_to(WORKSPACE)))
    ok = occurrences == ["config.py"]
    check(
        "5_6_paper_endpoint_single_source_of_truth",
        ok,
        f"literal found in: {occurrences}",
    )


def test_7_no_vix_swing_bot_reference():
    hits = []
    for p in _py_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in FORBIDDEN_PATH_REFS:
            if bad in text:
                hits.append(f"{p.relative_to(WORKSPACE)}: {bad!r}")
    check("7_no_vix_swing_bot_reference", len(hits) == 0, "; ".join(hits))


def test_8_no_quarantine_reference():
    hits = []
    for p in _py_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in FORBIDDEN_QUARANTINE_REFS:
            if bad in text:
                hits.append(f"{p.relative_to(WORKSPACE)}: {bad!r}")
    check("8_no_quarantine_reference", len(hits) == 0, "; ".join(hits))


def test_9_mutating_methods_only_in_execution_engine():
    """
    Scans line-by-line rather than whole-file-substring, skipping lines
    that start with "def " - a test function named e.g.
    "test_never_calls_submit_order():" contains the literal substring
    "submit_order(" as part of its own name, which is not a call site.
    This exact false positive recurred three times (Phase 4/6/7) before
    being fixed here at the source instead of relying on remembering to
    avoid the naming pattern in every future test file.
    """
    hits = []
    for p in _py_files():
        if p.name == MUTATING_METHODS_ALLOWED_FILE:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("def "):
                continue
            for pattern in MUTATING_METHOD_PATTERNS:
                if pattern in line:
                    hits.append(f"{p.relative_to(WORKSPACE)}:{lineno}: {pattern!r}")
    check("9_mutating_methods_only_in_execution_engine", len(hits) == 0, "; ".join(hits))


CALL_SITE_PREFIX = "trading_client."  # the real call-site form; excludes docstring prose like "TradingClient.submit_order()"


def test_10_mutating_calls_gated_behind_live_mode_check():
    """
    Every function in execution_engine.py that calls a mutating method
    (submit_order, cancel_order_by_id, ...) must have that call site
    textually preceded, WITHIN THE SAME FUNCTION BODY, by an
    `if mode != "LIVE":` early return, and that function's own signature
    must default mode to "DRY_RUN". Per-function (not whole-file) so this
    stays correct as more guarded functions (submit_and_confirm,
    cancel_and_confirm, ...) are added - a whole-file first-occurrence
    check would let a later function's call site "borrow" an earlier
    function's guard and pass incorrectly.

    This does not prove the guard is semantically correct in every
    possible refactor, but it does fail loudly if a guard, or the safe
    default, is ever deleted or misplaced relative to its own call site.
    """
    target = WORKSPACE / MUTATING_METHODS_ALLOWED_FILE
    if not target.exists():
        check("10_mutating_calls_gated_behind_live_mode_check", True, "execution_engine.py not present yet")
        return

    text = target.read_text(encoding="utf-8", errors="ignore")
    func_re = re.compile(r"^def (\w+)\(([^)]*)\):", re.MULTILINE)
    matches = list(func_re.finditer(text))

    problems = []
    any_mutating_call_found = False

    for i, m in enumerate(matches):
        func_name = m.group(1)
        func_signature = m.group(2)
        body = text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]

        called_patterns = [p for p in MUTATING_METHOD_PATTERNS if f"{CALL_SITE_PREFIX}{p}" in body]
        if not called_patterns:
            continue
        any_mutating_call_found = True

        has_safe_default = 'mode="DRY_RUN"' in func_signature or "mode='DRY_RUN'" in func_signature
        guard_idx = body.find('if mode != "LIVE":')

        if not has_safe_default:
            problems.append(f"{func_name}: no mode=\"DRY_RUN\" default in its own signature")
        if guard_idx == -1:
            problems.append(f"{func_name}: no 'if mode != \"LIVE\":' guard found in its body")
        else:
            for pattern in called_patterns:
                call_idx = body.find(f"{CALL_SITE_PREFIX}{pattern}")
                if call_idx < guard_idx:
                    problems.append(f"{func_name}: {pattern} call appears before its own guard")

    if not any_mutating_call_found:
        problems.append("expected at least one mutating call site in execution_engine.py, found none")

    check("10_mutating_calls_gated_behind_live_mode_check", len(problems) == 0, "; ".join(problems))


def main():
    test_1_no_live_endpoint()
    test_2_no_live_credential_file_paths()
    test_3_no_live_credential_env_var_names()
    test_4_only_allowlisted_credential_names()
    test_5_6_paper_endpoint_single_source_of_truth()
    test_7_no_vix_swing_bot_reference()
    test_8_no_quarantine_reference()
    test_9_mutating_methods_only_in_execution_engine()
    test_10_mutating_calls_gated_behind_live_mode_check()

    print()
    all_ok = all(ok for _, ok, _ in RESULTS)
    print("NETWORK ACCESS = 0, API CALLS = 0 (pure source-text scan)")
    print(f"submit_order/cancel/exercise/close call sites allowed ONLY in: {MUTATING_METHODS_ALLOWED_FILE}")
    print("ALL PASS" if all_ok else "SOME FAILED")
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
