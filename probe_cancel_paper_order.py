#!/usr/bin/env python3
"""
Phase 5 - cancel probe for the specific open Paper order left over from
the Phase 4.1 probe (order_id=4bb1664b-7d19-44e4-a508-e67d28a6832d,
SPY260902C00766000, ACCEPTED, unfilled - market was closed at submission
time).

Does not add a new cancel_order_by_id call site - it calls
execution_engine.cancel_and_confirm(), the one place in the workspace
that call exists (see test_static_security.py test 9-10). Runs exactly
once and stops. Ends by listing open orders (GetOrdersRequest,
status=OPEN) so the result is independently verifiable, not just
self-reported.
"""
import config
import contract_discovery as discovery
import execution_engine as engine
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

TARGET_ORDER_ID = "4bb1664b-7d19-44e4-a508-e67d28a6832d"


def main():
    trading_client = discovery.make_trading_client()

    print("=" * 70)
    print("PHASE 5 - CANCEL PROBE")
    print(f"Endpoint: {config.PAPER_BASE_URL}")
    print(f"Target order_id: {TARGET_ORDER_ID}")
    print("=" * 70)

    before = trading_client.get_order_by_id(TARGET_ORDER_ID)
    print(f"\n[BEFORE] status={discovery.field(before, 'status')} "
          f"symbol={discovery.field(before, 'symbol')} filled_qty={discovery.field(before, 'filled_qty')}")

    print("\n[EXECUTION] calling execution_engine.cancel_and_confirm(mode='LIVE') - ONE cancel, no retry loop")
    result = engine.cancel_and_confirm(TARGET_ORDER_ID, trading_client, mode="LIVE")

    print("\n[STATE MACHINE LOG]")
    for line in result["log"]:
        print(f"  {line}")

    print(f"\n[RESULT] state={result['state']}")
    if result["state"] != "CANCELED":
        print(f"  reason={result.get('reason')}")

    after = trading_client.get_order_by_id(TARGET_ORDER_ID)
    print(f"\n[AFTER] status={discovery.field(after, 'status')}")

    print("\n[OPEN ORDERS CONFIRMATION] get_orders(status=OPEN)")
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    print(f"  open order count: {len(open_orders)}")
    for o in open_orders:
        print(f"  STILL OPEN: id={discovery.field(o, 'id')} symbol={discovery.field(o, 'symbol')} "
              f"status={discovery.field(o, 'status')}")

    print("\n" + "=" * 70)
    print("CANCEL PROBE COMPLETE - stopping (no further orders will be submitted or canceled).")
    print("=" * 70)


if __name__ == "__main__":
    main()
