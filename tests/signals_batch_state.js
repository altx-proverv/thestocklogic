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
console.log('-'.repeat(78));
console.log('SIGNALS BATCH STATE: ' + (ok ? 'correct' : '*** DEFECTIVE ***'));
process.exit(ok ? 0 : 1);
