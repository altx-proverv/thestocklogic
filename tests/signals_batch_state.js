/* Scenario test for signals.html's batch-freshness logic.
 *
 * The functions are EXTRACTED FROM THE PAGE, not copied here. A copy would pass
 * forever while the page drifted, which is the failure mode this whole area
 * keeps producing.
 *
 *   node tests/signals_batch_state.js
 */
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'signals.html'), 'utf8');

function extract(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('signals.html no longer defines ' + name +
                             ' — the batch-freshness logic has moved');
  // brace-match from the first { after the signature
  let j = src.indexOf('{', i), depth = 0, k = j;
  for (; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (!depth) break; }
  }
  return src.slice(i, k + 1);
}

eval(extract('nextTD'));
eval(extract('weekdayOrNext'));

// The rule under test, stated once here and asserted below. It mirrors the page:
// before ~19:00 IST the current batch is the one for today; after the 18:35
// chain, for the next session.
function verdict(today, istMin, batchDate) {
  const sessionDate = nextTD(batchDate);
  const expected = (istMin >= 19 * 60) ? nextTD(today) : weekdayOrNext(today);
  return { sessionDate, expected, stale: sessionDate < expected };
}

const cases = [
  // [label, today, istMin, batchDate, expectStale, expectedSession]
  ['the incident: nothing published Mon evening',
   '2026-09-28', 20 * 60, '2026-09-25', true,  '2026-09-29'],
  ['still nothing next morning',
   '2026-09-29', 10 * 60, '2026-09-25', true,  '2026-09-29'],
  ['Monday batch, read Tuesday morning',
   '2026-09-29', 10 * 60, '2026-09-28', false, '2026-09-29'],
  ['batch published tonight, read tonight',
   '2026-09-28', 20 * 60, '2026-09-28', false, '2026-09-29'],
  // MUST NOT FLAG: between the 15:15 close and the 18:35 chain there is
  // legitimately no newer batch. Flagging it would cry wolf every afternoon.
  ['after the close, before the chain',
   '2026-09-28', 16 * 60, '2026-09-25', false, '2026-09-28'],
  ['Friday evening batch is Monday\'s watchlist',
   '2026-09-25', 20 * 60, '2026-09-25', false, '2026-09-28'],
  ['read on Sunday',
   '2026-09-27', 11 * 60, '2026-09-25', false, '2026-09-28'],
  // a long gap must still resolve to the next session, not drift
  ['a week of nothing',
   '2026-10-05', 10 * 60, '2026-09-25', true,  '2026-10-05'],
];

global.window = {};
eval(extract('counterTrend'));
eval(extract('regimeBanner'));

let ok = true;
console.log('BATCH FRESHNESS'.padEnd(46) + 'expected     stale');
console.log('-'.repeat(78));
for (const [label, today, istMin, batch, wantStale, wantExpected] of cases) {
  const v = verdict(today, istMin, batch);
  const good = v.stale === wantStale && v.expected === wantExpected;
  ok = ok && good;
  console.log('  ' + label.padEnd(44) + v.expected.padEnd(13) +
              String(v.stale).padEnd(7) + (good ? 'ok' : '** WRONG **'));
}

/* The regime is CONTEXT, not a filter. A counter-trend signal is de-emphasised
 * and marked, never dropped -- dropping it would make the page mirror ATLAS's
 * trading rules instead of reporting the market, which is the whole point of
 * the change. */
console.log('');
console.log('REGIME AS CONTEXT'.padEnd(46) + 'LONG ctr  SHORT ctr');
console.log('-'.repeat(78));
const regimeCases = [
  ['bearish',  true,  false],
  ['BEARISH',  true,  false],   // case-insensitive
  ['bullish',  false, true ],
  ['sideways', false, false],   // favours neither
  ['mixed',    false, false],
  ['',         false, false],   // unreadable regime marks nothing
];
for (const [reg, wantLong, wantShort] of regimeCases) {
  window._regimeNow = reg;
  const gl = counterTrend('LONG'), gs = counterTrend('SHORT');
  const good = gl === wantLong && gs === wantShort;
  ok = ok && good;
  console.log('  ' + ("regime '" + reg + "'").padEnd(44) +
              String(gl).padEnd(10) + String(gs).padEnd(10) +
              (good ? 'ok' : '** WRONG **'));
}

console.log('');
console.log('THE BANNER NAMES THE REGIME AND DISCLAIMS THE INSTRUCTION');
console.log('-'.repeat(78));
const bannerCases = [
  ['bearish',  ['REGIME BEARISH',  'against', 'not instructions']],
  ['bullish',  ['REGIME BULLISH',  'against']],
  ['sideways', ['REGIME SIDEWAYS', 'cash']],
  ['',         ['REGIME UNKNOWN',  'could not be read']],
];
for (const [reg, needles] of bannerCases) {
  window._regimeNow = reg;
  const html = regimeBanner();
  const missing = needles.filter(n => !html.includes(n));
  const good = missing.length === 0;
  ok = ok && good;
  console.log('  ' + ("regime '" + reg + "'").padEnd(44) +
              (good ? 'ok' : '** missing: ' + missing.join(', ') + ' **'));
}
// A bearish banner must not imply ATLAS will short.
window._regimeNow = 'bearish';
const bear = regimeBanner();
const noInstruction = bear.includes('ATLAS') && bear.includes('neither');
ok = ok && noInstruction;
console.log('  ' + 'bearish banner says ATLAS takes neither side'.padEnd(44) +
            (noInstruction ? 'ok' : '** IMPLIES AN INSTRUCTION **'));

console.log('-'.repeat(78));
console.log('SIGNALS BATCH STATE: ' + (ok ? 'correct' : '*** DEFECTIVE ***'));
process.exit(ok ? 0 : 1);
