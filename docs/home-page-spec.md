# Home Page — The Market Ecosystem

**Status:** design approved 17 Oct 2026, build not started
**Supersedes:** the separate Equity and Flash pages. Equity and Flash merge into one public home page.
**Design tokens:** `claude/equity-heatmap-spec.md` §08 — surfaces, ink, accents, the seven-step heat scale, type scale, layout grid. That section is the implementation contract and is unchanged.
**Also read:** `claude/equity-heatmap-spec.md` §01–§07 for why buy/sell content is removed, what is removed, the universe history and the language rules. `claude/radar-spec.md` for the event detector feeding the Flash column.

---

## 01 — The organising idea

The page is laid out as the market's causal chain, and it reads top to bottom in that order: **the reading → index levels → stocks moving → sector rotation and events → the calendar.**

An Indian session is caused in that direction. The index sets which sectors rotate; rotation decides which stocks move; events interrupt all of it. Every other market page in India is ordered "indices, top gainers, top losers, news" — a list of widgets rather than a chain of causes. This ordering is the page's point of view and should not be rearranged for convenience.

**"One view" is a constraint, not a slogan.** On a desktop the working floor has a fixed height and scrolls internally. A visitor should be able to take in the market's whole state without scrolling the page.

An outer ring of global cues — GIFT Nifty, US index futures, Brent, gold — belongs at the head of this chain and is **deliberately absent**. See §07.

---

## 02 — Structure, top to bottom

1. **Header.** Wordmark, nav, session state dot and clock. Nav is **Home · Meridian · Learn**. No Equity item, no Flash item, no archive link.
2. **The reading.** One sentence of market state in plain English, set large. Regime and since-when, breadth, how many sectors are green, which are leading and which are under pressure. Computed, not written by hand. Leading and lagging names come from the sector table itself, so the sentence can never disagree with the grid beneath it.
3. **Index levels.** Six tiles: NIFTY 50, BANK NIFTY, NIFTY MIDCAP 100, NIFTY SMALLCAP 100, INDIA VIX, USD/INR.
4. **The tape.** A continuously scrolling strip. §04.
5. **The working floor.** Two zones side by side: sector rotation on the left, the Flash column on the right. §05 and §06.
6. **Calendar.** Two columns — *Scheduled today* and *Landed yesterday*.
7. **Footer disclosure.** Per `equity-heatmap-spec.md` §07.

---

## 03 — The reading

Generated from the data, never hand-written. Shape:

> **Risk-on** since 8 October. Breadth 1,312 to 806, and 8 of 12 sectors are green. **IT and AUTO** are leading; **MEDIA and PSU BANK** are under pressure.

Leading and lagging sectors are the top two and bottom two of the same sector table the grid renders. Regime and breadth come from the existing nightly chain. Below it, one line of provenance: how often the reading recomputes, and how many events have been logged today. Out of hours it reads as at the last close instead.

---

## 04 — The tape

One scrolling strip carrying two kinds of item, mixed:

**Opening-range breaks.** ATLAS's range breaker already computes a fixed 09:15–10:00 opening range for every symbol and detects a one-minute close outside it. Expose that read-only to the page. Each item carries direction, the range itself, and the minute it broke:

> `↓ GRINDWELL −1.54% [ORB ↓] broke below its opening range 1,879.00–1,906.90 at 10:04`

This is the most distinctive thing on the page. Nothing retail-facing in India publishes opening-range breaks, and the computation already exists for the trading engine.

**Movers.** The existing rule: moved ±1.5% or more on the day **and** trading above 2× its own average volume, each with a factual reason — *above 20-day high*, *volume 2.6× average*, *reversed from day low*.

Rules: directional only, never a call. An up-arrow glyph rotated 180° for declines. The track is the item list concatenated with itself, translating 0 → −50% over 86s linear infinite, honouring `prefers-reduced-motion`. It runs continuously, is overwritten with fresh data each morning between 09:15 and 09:35, accumulates through the session, and out of hours scrolls the last session's closing state labelled as such.

---

## 05 — Sector rotation (centre zone)

Four columns on desktop, three below 1100px, two below 760px, one below 620px.

**Collapsed box:** sector name, aggregate % change, advancing/declining counts, and — this is what makes a sector-level view carry stock-level signal — **its top three constituents named inside the box with their percentages**, plus a "+N more" line. A reader sees where the action is at stock level without clicking anything.

**Expanded:** the box takes the full width of the centre zone at its own grid position. Every constituent renders as a tile with symbol, % change and last price. One sector open at a time.

**The space adjustment, which is a requirement and not a detail.** Expansion happens **inside the centre zone only**. The reading, the index strip, the tape, the Flash column and the calendar must not move when a sector opens. The centre zone owns its own scroll container. The clicked box stays pinned under the cursor: measure its position before the re-render, re-measure after, and correct the zone's `scrollTop` by the difference.

**The stock detail opens inside the expanded sector**, below its tiles — never at the foot of the page. Focus must not travel away from what was just clicked. Fields are descriptive only: last price, change and % change, day's range with a marker for where price sits in it, volume and volume against the symbol's own average, 20-day and 52-week highs, and one plain sentence of observed behaviour.

**Stale tiles** render dashed amber and must distinguish two cases: `NEW LISTING / building history` and `INVESTIGATE / no data`. HEGAM is a new listing and will read stale until roughly 1 December; it must never look like a fault.

---

## 06 — The Flash column (right zone)

Flash stops being a page and becomes this column. The name is kept — it describes an interrupting event far better than it described a page.

Three blocks, in order:

**1. Event cards.** From Radar. Each carries symbol, timestamp, % change, one factual sentence of what happened, and the source. Where a disclosure exists it is **a link that opens the primary document externally, in a new tab**, with the source named. We point at filings, we never proxy or republish them. Where no disclosure explains the move the card says **"No disclosed trigger found"** — which is the honest answer and should be the common one.

**2. Breadth through the session.** A sparkline of advancing symbols from the open to now, with a dashed line marking an even split, then the live advance/decline counts and the change since 09:15, then a sentence naming where it opened and whether it has improved or decayed. The page's single breadth number says where breadth is; this says where it is going, and those are different markets.

**3. Heavy volume, little movement.** Symbols trading above 3× their own average volume while holding inside ±0.5% on the day. Everything else on the page is triggered by price moving; this is the inverse — volume arriving before price does. It is the only block here not caused by a price move, and almost nothing retail-facing in India surfaces it.

**Do not put a static movers list in this column.** It duplicates the tape. The tape is the right format for a stream.

---

## 07 — Global cues: deliberately absent, and the condition for adding them

GIFT Nifty, Dow and Nasdaq futures, Brent crude and MCX gold belong at the head of the causal chain. They are **not on the page** because we have no feed for them.

What we established on 17 October:

| Cue | Status |
|---|---|
| USD/INR | **Available** — Zerodha currency segment. On the page, in the index strip. |
| MCX gold, MCX crude | Only if the Kite account has the MCX commodity segment enabled. MCX crude is WTI-linked, not Brent — a different contract. Unverified. |
| GIFT Nifty | No source. Trades on NSE IX and does not come through Kite's normal NSE feed. |
| Dow / Nasdaq futures | No source. CME contracts; no Indian broker feed carries them. |

A first version of this design drew all six as tiles. That was wishful — the design held slots for data with no plan behind it, and a page that looks finished while quietly having five holes in it is worse than a page that starts where its data starts.

**The condition for adding the band:** the box already reaches the open internet every night to pull the NSE bhavcopy, so this is a fetch job in an existing pattern rather than a subscription. Establish whether a free source for US futures, Brent and GIFT Nifty is actually reachable and stable from the box. If yes, it is a cron entry and the band returns at the head of the page. If no, it is a vendor cost and a commercial decision, not an engineering one. Either way the page ships without it.

---

## 08 — What has to be built that does not exist yet

| Item | Cost | Note |
|---|---|---|
| **Intraday breadth snapshots** | small build | The nightly chain gives a daily advance/decline from the bhavcopy. The curve needs a count across the traded universe snapshotted every few minutes through the session and stored. New table plus a cron entry. |
| **Opening-range breaks, exposed read-only** | small | ATLAS's breaker already computes the range and the break. The page needs a read-only view of it. **The page must never be able to write breaker state or stand ATLAS down.** |
| **Heavy-volume-no-move filter** | nearly free | Same `volume ÷ own average` the mover rule already computes. Only the filter inverts: high volume **and** small move. |
| **Calendar source** | planned | Scheduled events and what landed yesterday. This is the same work as Radar's regulator feeds, already on the charter. |

---

## 09 — Constraints that do not move

**Refresh during market hours:** tape every 15 seconds, heatmap every 30 seconds.

**Symbol source is `SYMBOL_SECTOR_MAP`**, never a glob of the price directory. There were 19 orphan parquets and a globbing page draws a tile for each.

**Outside market hours** the page shows the last completed session's close and says so on its face. It does not blank out and does not pretend to be live.

**The language rules of `equity-heatmap-spec.md` §07 apply to the whole page** — the tape, the event cards, the detail row, every tooltip and aria label. No buy, sell, entry, target, stop loss, recommendation, call, tip, or grade. Enforced by a test against the template actually served, not by care.

**Mandatory quality gate:** `bash check_js.sh` from `~/thestocklogic` before any commit touching an HTML file.
