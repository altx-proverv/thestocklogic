/* Scenario test for signals.html's live-view freshness logic.
 *
 * The functions are EXTRACTED FROM THE PAGE, not copied here. A copy would pass
 * forever while the page drifted, which is the failure mode this area keeps
 * producing -- see signals_batch_state.js, same discipline.
 *
 * What is under test is the one thing that decides whether a reader is shown a
 * price as current: liveState(). Getting it wrong in the permissive direction
 * means a twenty-minute-old quote rendered as LIVE, which is worse than showing
 * nothing, because the reader cannot tell.
 *
 *   node tests/signals_live_view.js
 */
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'signals.html'), 'utf8');

function extract(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('signals.html no longer defines ' + name +
                             ' — the live-view freshness logic has moved');
  let j = src.indexOf('{', i), depth = 0, k = j;
  for (; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (!depth) break; }
  }
  return src.slice(i, k + 1);
}

eval(extract('liveState'));
eval(extract('fmtAge'));

const OPEN = 10 * 60;            // 10:00 IST, mid-session
const BEFORE = 9 * 60 + 10;      // 09:10, before the loop starts
const AFTER = 15 * 60 + 40;      // 15:40, after it stops
const EDGE_OPEN = 9 * 60 + 20;   // the first minute of the window
const EDGE_SHUT = 15 * 60 + 20;  // the first minute outside it

function rows(ageSeconds, n) {
  const at = new Date(Date.now() - ageSeconds * 1000).toISOString();
  return Array.from({ length: n || 1 }, (_, i) => ({
    symbol: 'S' + i, direction: 'LONG', cycle_at: at,
  }));
}

const cases = [
  // [label, rows, istMin, expected kind]
  ['fresh quote mid-session',              rows(12, 3),   OPEN,      'live'],
  ['one cycle late is still live',         rows(75, 3),   OPEN,      'live'],
  ['two cycles late is still live',        rows(170, 3),  OPEN,      'live'],
  ['three minutes is the cut',             rows(181, 3),  OPEN,      'stale'],
  ['twenty minutes mid-session',           rows(1200, 3), OPEN,      'stale'],
  ['fresh data but before the window',     rows(12, 3),   BEFORE,    'closed'],
  ['last session, read after close',       rows(7200, 3), AFTER,     'closed'],
  ['no rows at all, market open',          [],            OPEN,      'empty'],
  ['no rows at all, market shut',          [],            AFTER,     'closed'],
  ['rows with unparseable timestamps',     [{symbol:'X',cycle_at:'not-a-date'}], OPEN, 'empty'],
  ['the first minute of the window',       rows(30, 2),   EDGE_OPEN, 'live'],
  ['the first minute after it',            rows(30, 2),   EDGE_SHUT, 'closed'],
];

let ok = true;
console.log('');
console.log('LIVE-VIEW FRESHNESS — three states, and the boundary between them');
console.log('-'.repeat(78));
for (const [label, r, istMin, want] of cases) {
  const got = liveState(r, istMin);
  const good = got.kind === want;
  ok = ok && good;
  const age = got.ageS == null ? '—' : got.ageS + 's';
  console.log(
    '  ' + label.padEnd(40) +
    got.kind.padEnd(9) +
    ('age ' + age).padEnd(12) +
    (good ? 'ok' : '** want ' + want + ' **'));
}

// The age label is what the reader actually sees, so it is asserted too: a stale
// banner that says "0 min ago" is a stale banner nobody believes.
console.log('');
console.log('  AGE LABELS');
const ages = [[0, '0s ago'], [45, '45s ago'], [60, '1 min ago'],
              [181, '3 min ago'], [3600, '1h 0m ago'], [7380, '2h 3m ago'],
              [null, '—']];
for (const [s, want] of ages) {
  const got = fmtAge(s);
  const good = got === want;
  ok = ok && good;
  console.log('    ' + String(s).padEnd(10) + got.padEnd(14) +
              (good ? 'ok' : '** want ' + want + ' **'));
}

// A stale or closed state must never be rendered as current. The page expresses
// that through lv-dim, so the renderer is checked for it rather than trusted.
console.log('');
console.log('  THE RENDERER MARKS NON-CURRENT DATA');
const render = extract('buildLiveView');
for (const [label, needle] of [
  ['stale sets the dimmed treatment', "st.kind==='stale'"],
  ['closed sets it too', "st.kind==='closed'"],
  ['there is a dimmed class to set', 'lv-dim'],
  ['stale says the prices are NOT current', 'NOT current'],
  ['closed says it is not a live price', 'not a live price'],
  ['watch-only rows are labelled', 'watchlist only'],
  ['actionable rows are labelled', 'actionable'],
  ['held rows are labelled', 'IN POSITION'],
  ['and nothing here counts in the record', 'accuracy record'],
]) {
  const good = render.indexOf(needle) >= 0;
  ok = ok && good;
  console.log('    ' + label.padEnd(44) + (good ? 'ok' : '** MISSING: ' + needle + ' **'));
}

// The fetch must be ordered by distance and must not pull the whole row.
console.log('');
console.log('  THE FETCH');
const fetchFn = extract('fetchLiveZones');
for (const [label, cond] of [
  ['nearest first', fetchFn.indexOf('order=dist_pct.asc') >= 0],
  ['named columns, not select=*', fetchFn.indexOf('select=*') < 0],
  ['reads the freshness column', fetchFn.indexOf('cycle_at') >= 0],
  ['reads the actionable flag', fetchFn.indexOf('publication_kind') >= 0],
]) {
  ok = ok && cond;
  console.log('    ' + label.padEnd(44) + (cond ? 'ok' : '** WRONG **'));
}

// The poll must stop outside market hours: a tab left open overnight should cost
// nothing, and a page that polls a dead loop every minute is just noise.
console.log('');
console.log('  THE POLL STOPS WHEN THE ENGINE DOES');
const poll = extract('startLiveViewPoll');
for (const [label, cond] of [
  ['guards the window start', poll.indexOf('9*60+20') >= 0],
  ['guards the window end', poll.indexOf('15*60+20') >= 0],
  ['skips weekends', poll.indexOf('dow===0') >= 0 && poll.indexOf('dow===6') >= 0],
  ['one timer only', poll.indexOf('if(_lvTimer) return') >= 0],
  ['on the cycle cadence, not faster', poll.indexOf('60*1000') >= 0],
]) {
  ok = ok && cond;
  console.log('    ' + label.padEnd(44) + (cond ? 'ok' : '** WRONG **'));
}

console.log('-'.repeat(78));
console.log('LIVE VIEW:', ok ? 'correct' : '*** DEFECTIVE ***');
process.exit(ok ? 0 : 1);
