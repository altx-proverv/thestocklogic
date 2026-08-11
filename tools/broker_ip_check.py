"""
Zerodha connectivity / IP-whitelist check
=========================================
Was an untracked iptest.py at the repo root. Two problems with that: it was one
`git clean -fd` from being lost, and it called kite.place_order() directly at
import time with no guard -- bypassing atlas.execution.broker.place_order and
therefore the kill switch -- so running it by accident placed a real order.

Kite Connect only enforces the IP whitelist on ORDER endpoints. Read calls
(profile, margins, ltp) succeed from any IP, so they cannot prove the gate is
open. That is why the original placed an order at all, and why this still can.

    python3 tools/broker_ip_check.py                  # read-only, default
    python3 tools/broker_ip_check.py --place-order    # places a REAL order

--place-order submits an AMO BUY for 1 share of RELIANCE at a limit 10% below
the last price -- far enough out that it parks in the book and cannot fill --
then immediately cancels it. That is the only way to prove the whitelist is
open. It is deliberately not the default.

Requires ZERODHA_API_KEY and a stored access token (broker_tokens in Supabase).
"""

import sys, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from atlas.execution.broker import get_kite

SYMBOL   = "RELIANCE"
EXCHANGE = "NSE"
DISCOUNT = 0.90     # limit this far below LTP so the order cannot fill
TAG      = "ipcheck"


def read_only_checks(kite) -> bool:
    """Auth and reachability. Does NOT prove the order IP gate is open."""
    ok = True
    try:
        p = kite.profile()
        print(f"  profile   OK   user: {p.get('user_name', '?')}")
    except Exception as e:
        ok = False
        print(f"  profile   FAIL {type(e).__name__}: {e}")
    try:
        m = kite.margins()
        bal = m.get("equity", {}).get("available", {}).get("live_balance")
        print(f"  margins   OK   live_balance: {bal}")
    except Exception as e:
        ok = False
        print(f"  margins   FAIL {type(e).__name__}: {e}")
    try:
        instr = f"{EXCHANGE}:{SYMBOL}"
        ltp = kite.ltp([instr])[instr]["last_price"]
        print(f"  ltp       OK   {SYMBOL}: {ltp}")
    except Exception as e:
        ok = False
        print(f"  ltp       FAIL {type(e).__name__}: {e}")
    return ok


def order_check(kite) -> bool:
    """Place an unfillable AMO order, then cancel it. Proves the IP gate."""
    instr = f"{EXCHANGE}:{SYMBOL}"
    ltp   = kite.ltp([instr])[instr]["last_price"]
    price = round(ltp * DISCOUNT, 1)
    print(f"  placing AMO BUY 1 {SYMBOL} @ {price} (LTP {ltp}) — cannot fill")

    order_id = None
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_AMO,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol=SYMBOL,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=1,
            product=kite.PRODUCT_CNC,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=price,
            validity=kite.VALIDITY_DAY,
            tag=TAG,
        )
        print(f"  PASS — IP gate open. order_id: {order_id}")
        return True
    except Exception as e:
        print(f"  FAIL — {type(e).__name__}: {e}")
        return False
    finally:
        # Cancel in a finally block. The original cancelled on the success path
        # only, so any exception after placement left a live order resting.
        if order_id:
            try:
                kite.cancel_order(variety=kite.VARIETY_AMO, order_id=order_id)
                print(f"  cancelled {order_id} — clean")
            except Exception as e:
                print(f"  !! COULD NOT CANCEL {order_id}: {e}")
                print(f"  !! Cancel it by hand in Kite before trading.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--place-order", action="store_true",
                    help="place and cancel a real unfillable order (the only "
                         "way to verify the IP whitelist)")
    args = ap.parse_args()

    kite = get_kite()
    if kite is None:
        print("No authenticated Kite session — complete the login first.")
        return 1

    print("=== read-only checks ===")
    ok = read_only_checks(kite)

    if not args.place_order:
        print("\nRead-only checks complete. These do NOT prove the order IP "
              "whitelist is open —\nKite enforces it on order endpoints only. "
              "Re-run with --place-order to verify that.")
        return 0 if ok else 1

    print("\n=== order check (places a REAL order) ===")
    return 0 if (ok and order_check(kite)) else 1


if __name__ == "__main__":
    sys.exit(main())
