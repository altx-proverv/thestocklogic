# HOME PAGE — DESIGN CONTRACT

Saved verbatim from the approved prompt of 2026-10-17. An earlier copy of this
file was incomplete and was removed; this replaces it. Future prompts read the
contract from here.

## Organising idea
Laid out as the market's causal chain, top to bottom: the reading → index levels →
stocks moving → sector rotation and events → calendar. Not "indices, gainers, losers,
news". Do not rearrange. On desktop the working floor has a fixed height and scrolls
internally — a reader takes in the whole market state without scrolling the page.

## Structure
1 Header: wordmark, nav HOME · MERIDIAN · LEARN, state dot, clock
2 The reading
3 Index strip, six tiles
4 The tape
5 Working floor: sector grid left, Flash column right
6 Calendar: scheduled today / landed yesterday
7 Footer disclosure (foot, not a banner)

## The reading
Generated, never hand-written. "Risk-on since 8 October. Breadth 1,312 to 806, and 8 of
12 sectors are green. IT and AUTO are leading; MEDIA and PSU BANK are under pressure."
Leaders and laggards are the top two and bottom two of the same sector table the grid
renders. Below it one provenance line: refresh cadence and events logged today. Out of
hours, as at the last close.

## Index strip — exchange names, not colloquial
NIFTY 50 (256265) · NIFTY BANK (260105, NOT "Bank Nifty") · NIFTY MIDCAP 100 (256777) ·
NIFTY SMLCAP 100 (267017, NOT "Smallcap") · INDIA VIX (264969) · USDINR FUT (CDS near-
month, labelled as a FUTURE because no spot instrument exists and it carries basis)

## The tape
Never stops. Two item kinds mixed:
 - opening-range breaks: "↓ GRINDWELL −1.54% [ORB ↓] broke below its opening range
   1,879.00–1,906.90 at 10:04" — direction, the range, the minute
 - movers: ±1.5% on 2x average volume, with a factual reason
Directional only, never a call. Up-arrow glyph rotated 180° for declines. Track is the
list concatenated with itself, translating 0 → -50% over 86s linear infinite, honouring
prefers-reduced-motion. REFRESHES EACH MORNING 09:15–09:35, accumulates through the
session, and HOLDS THE LAST SESSION'S CAPTURED STATE until the next refresh. It does not
blank out of hours.

## Sector grid (centre zone)
4 columns desktop / 3 under 1100px / 2 under 760px / 1 under 620px.
Collapsed box: sector name, aggregate %, adv/dec counts, AND ITS TOP THREE CONSTITUENTS
NAMED WITH PERCENTAGES plus "+N more". That is what makes a sector view carry stock-level
signal — do not omit it.
Expanded: full width of the centre zone at its own grid position, every constituent as a
tile with symbol, % and last price. One sector open at a time.
THE SPACE ADJUSTMENT, a requirement: expansion happens INSIDE THE CENTRE ZONE ONLY. The
reading, index strip, tape, Flash column and calendar must not move. The centre zone owns
its own scroll container. The clicked box stays pinned under the cursor — measure its
viewport position before the re-render, re-measure after, correct scrollTop by the
difference.
Stock detail opens INSIDE the expanded sector, below its tiles, never at the page foot.
Fields, descriptive only: last price, change and %, day range with a marker for where
price sits in it, volume and volume vs own average, 20-day and 52-week highs, one plain
sentence of observed behaviour.
Stale tiles, dashed amber, TWO distinct labels: "NEW LISTING / building history" (HEGAM
reads stale until ~1 Dec and must not look like a fault) and "INVESTIGATE / no data".

## Flash column (right zone)
Three blocks in order:
1 Event cards — symbol, timestamp, %, one factual sentence, source. A disclosure is a
  link opening the PRIMARY DOCUMENT EXTERNALLY in a new tab, source named. Never proxy
  or republish a filing. No disclosure → "No disclosed trigger found".
2 Breadth through the session — sparkline of advancing symbols, dashed line at an even
  split, live adv/dec counts, change since 09:15, one sentence on where it opened and
  whether it improved or decayed.
3 Heavy volume, little movement — above 3x own average volume while inside ±0.5%.
NO static movers list in this column. It duplicates the tape.

## Language rules
Never, anywhere: buy, sell, entry, target, stop loss, recommendation, call, tip, grade
or conviction labels, or any phrasing telling a user what to do. Permitted: observed
facts about price, volume, range and sector strength. Footer states the page is market
data for information only, that TSL is not SEBI-registered as a Research Analyst or
Investment Adviser, and that nothing on it is investment advice.

## Tokens — exact values, this is the contract
Surfaces: ground #080F1C · header #0A1424 · card #0D1829 · rail #0C1626 · rail label
#101E33 · hairline rgba(232,237,245,0.1) · radius 10px (sector 7px, tile 5px)
Ink: primary #E8EDF5 · secondary #8A9BB4 · micro-label #7D8FA8 · tertiary #C4D2E4 ·
link #7FA8D9 / hover #A9C6E8
Accents, one job each: brand green #00FF88 — LOGO, ACTIVE NAV UNDERLINE, LIVE DOT ONLY ·
positive #4ADE80 · negative #F87171 · amber #E39F7F with border rgba(217,119,87,0.6) ·
closed dot #7D8FA8
HEAT SCALE on day % — deliberately NOT the brand green, so bright green never reads
as "up":
  >= +2.00%  #1E8A4C
  >= +1.00%  #19663D
  >= +0.30%  #15492F
  >  -0.30%  #1C2E4A   (neutral)
  >  -1.00%  #5E1A20
  >  -2.00%  #8A1F26
  <= -2.00%  #B4232B
Tile states: default border 1px solid rgba(8,15,28,0.45) · selected 2px solid #E8EDF5 ·
hover outline 2px solid rgba(232,237,245,0.45) offset -2px · tile ink #F8FAFC / sub
rgba(248,250,252,0.68) · stale fill #131C29, ink #9AA9BE, sub #78889D, border 1px dashed
rgba(227,159,127,0.75) · sector hover border rgba(232,237,245,0.28)
Type: IBM Plex Sans 400/500/600/700 for text, IBM Plex Mono 400/500/600 for EVERY
figure with font-variant-numeric: tabular-nums.
  wordmark 14px/700/0.14em/uppercase · nav 13px, active 600 + 2px #00FF88 underline ·
  micro-label 9.5px/600/0.14em · the reading clamp(19px,2.2vw,27px)/600/-0.012em,
  text-wrap balance, max 62ch · index name 10.5px/600/0.08em · index level
  20px/600/-0.01em · index change 12.5px/600 · sector name 10.5px/600/0.04em · sector %
  15px/600 · adv/dec 9.5px · named constituent 9.5px · tile symbol/%/price
  10px/12.5px/9.5px · tape symbol and figure 12px/600 · tape reason 11px #7D8FA8
Layout: content max-width 1480px centred; side gutter max(16px, env(safe-area-inset-
left/right,0px)); index strip repeat(6,minmax(0,1fr)) gap 10px; floor
minmax(0,1fr) 352px gap 12px and at >=1100px a fixed height clamp(440px,54vh,620px) with
the centre zone scrolling inside it; sector grid gap 7px; tiles
repeat(auto-fill,minmax(94px,1fr)) gap 6px.
Derived: detail change colour positive #4ADE80 / negative #F87171 / stale #9AA9BE ·
volume multiple >=1.5x renders #4ADE80 else #C4D2E4 · range sentence >=70% "Trading in
the upper third of today's range", <=30% "lower third", else "mid-range today" · live
state dot #00FF88 + "Live · HH:MM:SS IST" · closed state dot #7D8FA8 + "Market closed ·
last close <date>" + "Showing the close of <date>. Updates resume at 09:15."
Mobile: sector boxes stack to one column; a tile expands to full width IN PLACE — no
modal, no bottom sheet.

## Constraints
Symbol source is SYMBOL_SECTOR_MAP, never a glob of the price directory — there were 19
orphan parquets and a globbing page draws a tile for each. Out of hours the page shows
the last completed session's close and says so on its face; it does not blank out and
does not pretend to be live. Nothing is deleted from the database.

## Responsive — both are first-class, neither is an afterthought

THE PAGE BODY MUST NEVER SCROLL HORIZONTALLY, at any width. Any grid or flex child
holding running text, a figure or a table gets min-width: 0 so content wraps inside it
instead of pushing the page wider.

Side gutter at every width: padding-left: max(16px, env(safe-area-inset-left, 0px)) and
padding-right: max(16px, env(safe-area-inset-right, 0px)), set once on the outer
wrapper, with vertical padding written as padding-block so the shorthand never zeroes
the sides.

>= 1100px — the desk
  floor: two columns, minmax(0,1fr) 352px, FIXED height clamp(440px,54vh,620px), and
  the centre zone scrolls inside it. This is what makes "one view" true.
  index strip 6 across · sector grid 4 across · detail row 4 across

<= 1099px — tablet and large phone landscape
  *** THE FLOOR'S FIXED HEIGHT AND INNER SCROLL ARE REMOVED. ***
  The floor becomes one column in natural document flow: sector grid first, then the
  Flash column beneath it. A scroll region nested inside a page scroll is unusable on
  touch — do not keep it.
  index strip 3 across · sector grid 3 across · detail row 2 across

<= 760px
  sector grid 2 across

<= 620px — phone
  index strip 1 across · sector grid 1 across · calendar 1 column · detail row 1 column
  the detail row's price block drops below the symbol at full width rather than sitting
  right-aligned
  stock tiles repeat(auto-fill, minmax(98px, 1fr))
  a tile expands to full width IN PLACE — no modal, no bottom sheet, nothing to dismiss

The tape keeps scrolling at every width. It is the one element that does not reflow —
it is a horizontal scroll by design, inside its own container, and it never causes the
page body to scroll sideways.

Every sector box and stock tile is a real <button>. At phone width each must present at
least a 44px touch target in its short dimension, and keyboard focus must be visible on
all of them.

Build and check at three widths before committing: 1440, 820 and 390.
