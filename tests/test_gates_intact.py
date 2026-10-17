#!/usr/bin/env python3
"""
EVERY GATED PAGE STILL HAS ITS GATE.
====================================

WHY THIS EXISTS. On 2026-10-17 an anchor-to-anchor edit on home.html replaced
the span between a comment and the BOOT marker, and the auth block sat inside
that span. requireSession() and mountAccount() were deleted outright. The CALL
at the bottom of the file survived, so the page still said `await
requireSession()` -- it simply threw at load. /home was ungated, and it was
caught only because the page happened to throw where someone was looking. That
is the second time in one session an edit swallowed a neighbouring block.

SO A NAME-REFERENCE CHECK IS NOT ENOUGH, and that is the whole design of this
file: grepping for "requireSession" would have PASSED on the broken page,
because the surviving call still mentioned it. Each gate is therefore checked
three ways, and all three must hold:

  DECLARED   `function NAME(` exists          -- fails if the body is deleted
  CALLED     `NAME(` exists somewhere that is NOT the declaration
                                              -- fails if the boot call goes
  ENFORCING  the page's content is hidden until the gate reveals it, and the
             gate is the only thing that reveals it
                                              -- fails if the gate is bypassed

The third is the one that means something on its own: if the gate is gone,
nothing un-hides the content, so the page shows nothing rather than showing
everything.

PROVEN BY MUTATION at the end: for every page, deleting the declaration and
deleting the call are each shown to fail the check. A test for a deletion that
has never been shown to catch a deletion is a test nobody should trust.

No node, no network. The box has no node and this must run there too.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# Each gated page, and what its own gate looks like. The mechanisms genuinely
# differ -- /home and performance.html hide a shell, atlas mounts an account
# menu off a session it already resolves, signals renders a gate view, admin
# checks an email allowlist inline -- so this describes each rather than
# pretending they are the same.
GATES = {
    "home.html": {
        "route": "/home",
        "declare": ["requireSession", "mountAccount", "denied"],
        "call": ["requireSession", "mountAccount", "denied"],
        "hidden": "shell",
        "reveal": "requireSession",
    },
    "atlas.html": {
        "route": "/atlas",
        "declare": ["loadSession", "mountAccount"],
        "call": ["loadSession", "mountAccount"],
        "session_read": "auth.getSession",
    },
    "signals.html": {
        "route": "/dashboard",
        "declare": ["checkAuth", "setAuth", "renderGate", "signOut"],
        "call": ["checkAuth", "setAuth", "renderGate"],
        "session_read": "auth.getSession",
    },
    "performance.html": {
        "route": "/performance.html",
        "declare": ["pRequireSession", "pDeny"],
        "call": ["pRequireSession", "pDeny"],
        "hidden": "shell",
        "reveal": "pRequireSession",
    },
    "admin.html": {
        "route": "/admin",
        # No named gate function: admin checks an allowlist inline at boot.
        "declare": ["loadDashboard", "adminLogin"],
        "call": ["loadDashboard"],
        "session_read": "auth.getSession",
        "inline": ["_adminEmails", "adminView", "loginView"],
    },
}


def scripts_of(html: str) -> str:
    """Only the inline script bodies -- markup cannot declare a function."""
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html,
                                re.S | re.I))


def declared(js: str, name: str) -> bool:
    return bool(re.search(r"(?:async\s+)?function\s+" + re.escape(name)
                          + r"\s*\(", js))


def called(js: str, name: str) -> bool:
    """An invocation that is NOT the declaration.

    This is the half a grep gets wrong. On the broken page the call survived
    and the declaration did not, so a search for the bare name passed."""
    for m in re.finditer(re.escape(name) + r"\s*\(", js):
        before = js[max(0, m.start() - 24):m.start()]
        if re.search(r"function\s+$", before):
            continue                      # this occurrence IS the declaration
        return True
    return False


def hidden_by_default(html: str, el_id: str) -> bool:
    """The content element must carry `hidden` in the served markup."""
    m = re.search(r"<[^>]*id=[\"']" + re.escape(el_id) + r"[\"'][^>]*>", html)
    return bool(m) and "hidden" in m.group(0)


def revealed_only_in(js: str, el_id: str, fn: str) -> bool:
    """Every line that un-hides the shell sits inside the gate function."""
    body = fn_body(js, fn)
    if body is None:
        return False
    pat = re.compile(r"getElementById\(['\"]" + re.escape(el_id)
                     + r"['\"]\)\.hidden\s*=\s*false")
    everywhere = len(pat.findall(js))
    inside = len(pat.findall(body))
    return everywhere > 0 and everywhere == inside


def fn_body(js: str, name: str):
    """Source of one function, by brace matching from its declaration."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", js)
    if not m:
        return None
    i = m.end() - 1
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[i:j + 1]
    return None


def audit(html: str, spec: dict) -> list:
    """[] when the gate is intact, else the reasons it is not."""
    js = scripts_of(html)
    bad = []
    for n in spec.get("declare", []):
        if not declared(js, n):
            bad.append(f"{n}() is not declared")
    for n in spec.get("call", []):
        if not called(js, n):
            bad.append(f"{n}() is never called")
    if spec.get("session_read") and spec["session_read"] not in js:
        bad.append(f"no {spec['session_read']}() -- nothing reads a session")
    for marker in spec.get("inline", []):
        if marker not in js:
            bad.append(f"inline gate marker {marker!r} is gone")
    if spec.get("hidden"):
        if not hidden_by_default(html, spec["hidden"]):
            bad.append(f"#{spec['hidden']} is not hidden in the markup")
        if not revealed_only_in(js, spec["hidden"], spec["reveal"]):
            bad.append(f"#{spec['hidden']} is revealed outside "
                       f"{spec['reveal']}()")
    return bad


def main() -> int:
    ok = True

    print("EVERY GATED PAGE STILL HAS ITS GATE")
    print("-" * 104)
    for page, spec in GATES.items():
        p = ROOT / page
        if not p.exists():
            print(f"  ** {page} IS MISSING **")
            ok = False
            continue
        bad = audit(p.read_text(encoding="utf-8"), spec)
        ok &= not bad
        print(f"  {spec['route']:<20}{page:<20}"
              f"{'gate intact' if not bad else '** ' + '; '.join(bad) + ' **'}")

    print()
    print("AND IT CATCHES A DELETION — every gate, both halves")
    print("-" * 104)
    # THE MUTATION PROOF. For each page, delete the declaration (the exact
    # 2026-10-17 failure) and separately delete the call, and assert each is
    # caught. Without this the file is a claim, not a test.
    for page, spec in GATES.items():
        p = ROOT / page
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        name = spec["declare"][0]

        body = fn_body(scripts_of(src), name)
        decl = re.search(r"(?:async\s+)?function\s+" + re.escape(name)
                         + r"\s*\([^)]*\)\s*\{", src)
        if body is None or not decl:
            print(f"  {page:<20}** could not locate {name}() to mutate **")
            ok = False
            continue

        # (a) the declaration and its body, gone — the real failure
        gone_decl = src[:decl.start()] + src[decl.start() + len(decl.group(0))
                                             - 1 + len(body):]
        caught_a = bool(audit(gone_decl, spec))

        # (b) every call gone, declaration intact
        gone_call = re.sub(r"(?<!function )(?<!function  )\b"
                           + re.escape(name) + r"\s*\(", "NOOP_CALL(", src)
        gone_call = gone_call.replace("function NOOP_CALL(",
                                      f"function {name}(")
        caught_b = bool(audit(gone_call, spec))

        ok &= caught_a and caught_b
        print(f"  {page:<20}delete {name}() body -> "
              f"{'CAUGHT' if caught_a else '** MISSED **':<12}"
              f"delete its call -> {'CAUGHT' if caught_b else '** MISSED **'}")

    print()
    print("THE 2026-10-17 BREAKAGE ITSELF")
    print("-" * 104)
    # Reconstructed exactly: an anchor-to-anchor replacement that spanned from
    # a comment to the BOOT marker, taking the whole auth block with it. The
    # call at the bottom of the file survived, which is why a name search
    # passed and the page shipped ungated.
    home = (ROOT / "home.html").read_text(encoding="utf-8")
    a = home.find("/* ── AUTH ──")
    b = home.find("/* ── BOOT ──")
    if a == -1 or b == -1 or b <= a:
        print("  ** could not locate the AUTH..BOOT span to reconstruct **")
        ok = False
    else:
        broken = home[:a] + home[b:]
        still_named = "requireSession" in broken
        reasons = audit(broken, GATES["home.html"])
        caught = bool(reasons)
        ok &= caught and still_named
        print(f"  {'the surviving call still names requireSession':<58}"
              f"{'yes — a grep would pass' if still_named else 'no'}")
        print(f"  {'but the gate check fails':<58}"
              f"{'CAUGHT' if caught else '** MISSED **'}")
        for r in reasons[:4]:
            print(f"        {r}")

    print()
    print("AND A PAGE THAT NEVER HAD A GATE IS NOT SILENTLY PASSED")
    print("-" * 104)
    # index.html and waitlist.html are PUBLIC by design. If one ever grows a
    # shell that expects a gate, it belongs in GATES above -- this makes the
    # boundary explicit rather than assumed.
    for page in ("index.html", "waitlist.html"):
        p = ROOT / page
        if not p.exists():
            continue
        js = scripts_of(p.read_text(encoding="utf-8"))
        gated = any(declared(js, n) for n in
                    ("requireSession", "pRequireSession", "checkAuth"))
        good = not gated
        ok &= good
        print(f"  {page:<20}"
              f"{'public, no gate expected' if good else '** HAS A GATE BUT IS NOT IN GATES **'}")

    print("-" * 104)
    print("GATES:", "all intact" if ok else "*** A GATE IS MISSING ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
