#!/usr/bin/env python3
"""The market flash: the validator, the template, and the two fallback paths.

The validator is the only thing enforcing "never invent a number" and "never
recommend" -- the prompt asks, this decides -- so it is tested on both the cases
it must accept and the cases it must reject. A validator with no rejection test
is a validator nobody has seen say no.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import engine.market_flash as F                                   # noqa: E402

ok = True

def check(label, got, want, extra=""):
    global ok
    good = got == want
    ok &= good
    print(f"  {label:<52}{str(got):<10}"
          f"{'ok' if good else '** want ' + str(want) + ' **':<22}{extra}")

D = {"session_date": "2026-10-01", "regime": "mixed",
     "advancing": "211", "declining": "525", "stocks": "736",
     "breadth_pct": "28.7", "sectors_total": "12", "sectors_green": "2",
     "best": [("IT", "+0.04"), ("BANKING", "+0.01"), ("TELECOM", "-0.13")],
     "worst": [("AUTO", "-1.37"), ("METAL", "-0.93"), ("INFRA", "-0.56")],
     "watching": "158", "actionable": "41", "entries_today": "0"}

print("=" * 78)
print("THE VALIDATOR ACCEPTS A DESK NOTE")
print("-" * 78)
for label, t in [
    ("the shape asked for",
     "Breadth stays weak at 29% advancing. Auto leads the decline at -1.37%, "
     "with Metal and Infra close behind. IT and Banking are the only sectors "
     "holding green."),
    ("describing what ATLAS does is not advice",
     "Breadth is 28.7% advancing. ATLAS is watching 158 setups and following "
     "breadth."),
    ("rounding an input is not inventing",
     "Breadth stays weak at 29% advancing, 211 up against 525 down."),
    ("two sentences, no numbers at all",
     "Breadth is narrow across the tape. Only two sectors held green."),
]:
    got, why = F.validate(t, D)
    check(label, got, True, why)

print()
print("AND REJECTS EVERYTHING ELSE")
print("-" * 78)
for label, t, marker in [
    ("a figure that is not in the inputs",
     "Breadth is 28.7% advancing. Nifty fell 0.82% on the day.", "not in inputs"),
    ("a recomputed figure",
     "Breadth is 28.72% advancing.", "not in inputs"),
    ("a plausible but absent total",
     "Breadth is 28.7% advancing across 743 stocks.", "not in inputs"),
    ("advice: consider",
     "Breadth is 28.7%. Consider short exposure.", "advisory"),
    ("advice: an opportunity",
     "METAL at -0.93% is an opportunity.", "advisory"),
    ("advice: a forecast",
     "Breadth is 28.7%. We expect follow-through.", "advisory"),
    ("advice: risk-reward framing",
     "Breadth is 28.7% and the risk-reward is skewed.", "advisory"),
    ("advice: should",
     "Readers should note breadth at 28.7%.", "advisory"),
    ("four sentences", "A. B. C. D.", "sentences"),
    ("a wall of text", "word " * 70, "too long"),
    ("markdown", "Breadth is **28.7%** advancing.", "markdown"),
    ("an exclamation", "Breadth is 28.7% advancing!", "exclamation"),
    ("a question", "Is breadth 28.7%?", "question"),
    ("a headline on its own line", "FLASH\nBreadth is 28.7%.", "multi-line"),
    ("empty", "   ", "empty"),
]:
    got, why = F.validate(t, D)
    check(label, got, False, why)
    ok &= marker in why
    if marker not in why:
        print(f"      ** reason should name {marker!r}, said {why!r} **")

print()
print("THE TEMPLATE IS PERMANENT INFRASTRUCTURE, NOT A PLACEHOLDER")
print("-" * 78)
tpl = F.template(D)
print(f"  {tpl}")
tok, twhy = F.validate(tpl, D)
check("it passes the same validator the model must", tok, True, twhy)
check("  it quotes the breadth", "28.7%" in tpl, True)
check("  the weakest sector", "AUTO" in tpl and "-1.37" in tpl, True)
check("  and what ATLAS is watching", "158" in tpl, True)
# a thin input set must still produce something, not a crash or an empty string
thin = {"session_date": "2026-10-01", "regime": "mixed"}
tt = F.template(thin)
check("a near-empty input set still yields a line", bool(tt.strip()), True, tt[:40])
check("  which also validates", F.validate(tt, thin)[0], True)

print()
print("BOTH FALLBACK PATHS END IN THE TEMPLATE")
print("-" * 78)
import os
_saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
text, source, model, why = F.generate(D)
check("no API key -> template", source, "template")
check("  and no reject reason, it was never generated", why, None)

os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
class R:
    def __init__(s, c, j=None, t=""): s.status_code, s._j, s.text = c, j or {}, t
    def json(s): return s._j
_saved_post = F.requests.post

F.requests.post = lambda *a, **k: R(500, t="upstream error")
check("API 500 -> template", F.generate(D)[1], "template")

def _boom(*a, **k): raise TimeoutError("gone")
F.requests.post = _boom
check("API timeout -> template", F.generate(D)[1], "template")

# the one that matters: a well-formed response that breaks a rule
F.requests.post = lambda *a, **k: R(200, {"content": [
    {"type": "text", "text": "Breadth is 28.7% advancing and Nifty fell 0.82%."}]})
text, source, model, why = F.generate(D)
check("invented number -> template", source, "template")
ok &= why is not None and "not in inputs" in (why or "")
print(f"      reject reason stored: {why}")

F.requests.post = lambda *a, **k: R(200, {"content": [
    {"type": "text", "text": "Breadth is 28.7%. Consider shorts."}]})
text, source, model, why = F.generate(D)
check("advice -> template", source, "template")
ok &= why is not None and "advisory" in (why or "")

# and a clean one is kept
F.requests.post = lambda *a, **k: R(200, {"content": [
    {"type": "text", "text": "Breadth stays weak at 29% advancing. "
                             "AUTO leads the decline at -1.37%."}]})
text, source, model, why = F.generate(D)
check("a valid note is kept", source, "llm")
check("  and records the model", model, F.MODEL)

F.requests.post = _saved_post
if _saved_key is None:
    os.environ.pop("ANTHROPIC_API_KEY", None)
else:
    os.environ["ANTHROPIC_API_KEY"] = _saved_key

print()
print("THE PROMPT CARRIES ONLY WHAT WAS READ")
print("-" * 78)
p = F.build_prompt(D)
check("every DATA line is a figure we gathered", "nifty" in p.lower(), False,
      "no Nifty mark today, so the prompt does not mention it")
check("  breadth is there", "28.7% advancing" in p, True)
check("  so is the worst sector", "AUTO -1.37%" in p, True)
check("the model is Haiku", F.MODEL, "claude-haiku-4-5-20251001")

print("-" * 78)
print("MARKET FLASH:", "correct" if ok else "*** DEFECTIVE ***")
sys.exit(0 if ok else 1)
