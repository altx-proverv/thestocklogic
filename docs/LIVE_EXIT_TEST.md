# The live one-share exit test

ATLAS stays halted until this passes. Nothing else in the repo can replace it:
every other test runs against a stub, and the 2026-10-07 incident happened
because this exit path had never called the real Zerodha API.

**Run it yourself, with the market open.** Three phases, ordered by commitment.
Phase 1 and 2 open no position and can be run at any point in the session.
Phase 3 transacts.

---

## Before you start

```
cd /home/ubuntu/thestocklogic
venv/bin/python -m atlas.execution.zerodha_morning     # only if the token is stale
venv/bin/python -m tools.live_exit_test --preflight
```

**Pass looks like:** `READY` on the last line, with

```
  market hours            : True   (HH:MM IST)
  before the MIS cutoff   : True   (cutoff 15:12, engine exit 15:00)
  >45 min before cutoff   : True   (phase 3 needs the room)
  ATLAS halted            : True   ...
  broker session           : OK
  BHARTIARTL LTP            : <a number > 0>
  PRIMARY_STOP             : SL -> SL
  FALLBACK_EXIT            : LIMIT_THROUGH -> LIMIT
  independence contract    : PASS
```

`ATLAS halted : True` is **expected and required**. The exit path is deliberately
not halt-gated — entries are. If it says False, ATLAS is live and this is the
wrong time to be testing.

Best window: **10:00–14:00 IST**, on a normal session. Avoid the first fifteen
minutes (spreads) and anything after 14:15 (phase 3 needs room before 15:00).

---

## Phase 1 — does Zerodha accept our stop?

This is the single most important command in this document. It places a real
`SL` order 15% away from the market, reads it back from the broker, and cancels
it. Nothing can fill and no position is opened.

```
venv/bin/python -m tools.live_exit_test --phase 1 --live
```

**PASS:**

```
  ACCEPTED — order_id 2510090012345
  broker says: {"order_id": "...", "order_type": "SL", "price": <limit>,
                "product": "MIS", "quantity": 1, "status": "TRIGGER PENDING",
                "trigger_price": <trigger>, "transaction_type": "BUY", ...}
  cancelled 2510090012345

  PASS — status 'TRIGGER PENDING'
```

Three things have to be true in that output, not just the word PASS:

1. **`status` is `TRIGGER PENDING`.** The order is resting at the exchange. This
   is the state the 7 Oct stop never reached.
2. **`order_type` is `SL`** — not `SL-M`. If the broker echoes `SL-M`, something
   is rewriting the order and the independence contract is void.
3. **`trigger_price` and `price` are both present, and `price` > `trigger_price`**
   (a short's stop is a BUY, so the limit sits above the trigger).

**FAIL:** any `REJECTED by the real API` line. Copy the message verbatim —
especially if it mentions market protection, trigger price ranges, or
"not allowed via API". **Do not resume ATLAS.** That text is the specification
we are still missing.

**If it accepts but cannot cancel:** the script says so in capitals. Cancel the
order by hand in Kite before running anything else. A resting `SL` BUY can open
a short position on its own if price ever reaches the trigger.

---

## Phase 2 — does Zerodha accept our fallback?

Same idea for the marketable `LIMIT`. Priced 15% *below* the market so the
identical order type rests instead of filling.

```
venv/bin/python -m tools.live_exit_test --phase 2 --live
```

**PASS:** `ACCEPTED`, `broker says` shows `"order_type": "LIMIT"` with
`"status": "OPEN"`, then `cancelled`, then `PASS — status 'OPEN'`.

The line above it prints the price the *real* fallback would have used
(through the ask), so you can see that the order which rests here and the order
which fills in production differ only in the number.

**FAIL:** any rejection. This is the layer that is supposed to work when the
stop does not; a rejection here means there is still only one layer.

---

## Phase 3 — the whole chain, one share

This opens a real one-share MIS short, lets `exits.protect()` place the real
stop, reads the stop back from the broker, and closes the position with the real
fallback. Cost: brokerage plus the spread, call it ₹5–₹25 on BHARTIARTL.

```
venv/bin/python -m tools.live_exit_test --phase 3 --live
```

**PASS, in order:**

```
  [1/4] entering: {... "order_type": "MARKET", "market_protection": 3 ...}
        order_id <id>
        broker says: {... "status": "COMPLETE", ...}

  [2/4] exits.protect() with stop <price> (2% above a fill of <fill>)
        -> {"ok": true, "mechanism": "SL", "legs": {"stop_order_id": "<id>"}, ...}

  [3/4] reading the stop back from the broker:
        {"order_type": "SL", "status": "TRIGGER PENDING", "quantity": 1, ...}

  [4/4] closing with exits.emergency_exit() — the real fallback
        -> {"ok": true, "order_id": "<id>", "limit_price": <through the book>, ...}
        broker says: {"order_type": "LIMIT", "status": "COMPLETE", ...}

        cancelled the resting stop <id>

  positions in BHARTIARTL after the test: []

  PASS — stop placed: True, fallback exit: True, flat: True
```

**All three of these must hold:**

- `[2/4]` returned `"ok": true` with a real `stop_order_id` — the stop exists at
  the broker, which is exactly what did not happen on 7 Oct.
- `[3/4]` shows `TRIGGER PENDING` — it is resting, not merely accepted.
- `positions ... : []` and `flat: True` — **the account is flat.** Nothing
  overnight, nothing for the broker to square off.

**FAIL, and what to do:**

| What you see | What it means | Do this |
|---|---|---|
| `[2/4]` `"ok": false` | the stop was refused — 7 Oct again | the script still runs the fallback; if `[4/4]` is `ok: true` the design worked and you have the rejection text to fix |
| `[4/4]` `"ok": false` | **both layers failed** | **close the one share by hand in Kite immediately**, then report both messages |
| `COULD NOT CANCEL THE STOP` | a live stop rests against a closed position | cancel it by hand in Kite now — it can open a fresh short |
| `positions ... : [{'qty': -1, ...}]` | the exit did not fill | close by hand; the limit may have been priced inside the spread |

---

## What a full pass authorises

All three phases green means: the order shapes are accepted by the real API, the
stop actually rests at the exchange, and the fallback closes a real position.

That is the condition for resuming ATLAS. The resume is yours:

```
rm /var/lib/atlas/halt          # or send /resume
```

Run `venv/bin/python -m tools.verify_exit_path` once more after resuming — its
section 5 should then report the halt guards as *not* engaged, which is the
check that the guards are reading live state rather than a stale file.

## What it does not cover

- **A triggered stop.** Phase 1 and 3 prove the stop rests; neither proves what
  happens when price reaches the trigger and the limit has to fill. The first
  real stop-out is still the first real stop-out.
- **The 15:00 square-off.** `atlas-mis-squareoff.timer` is verified statically
  against the configured cutoff. Its first real firing on a day with an open MIS
  position is unobserved. Watch the journal that day:
  `journalctl -u atlas-mis-squareoff -n 50`.
- **A rejection we have not seen.** The stub enforces the two rejections from
  7 Oct and the documented contract. Zerodha has rules that are not in the docs;
  this is how we find the next one.
