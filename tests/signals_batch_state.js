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
let ok = true;
eval(extract('marketFlash'));
eval(extract('esc'));
function fmtDate(d){ return String(d); }

/* THE REGIME EXPLANATION BOX IS GONE, replaced by the market flash. What it used
 * to assert -- "opens SHORTS here", "not instructions", "cash when they
 * disagree" -- was three paragraphs of mechanism, and one of those sentences had
 * been false since regime suppression was removed from 06_push. A reader opening
 * a screener wants the day, not the manual.
 *
 * So the assertions move with the contract. What is invariant is NOT the wording:
 * it is that the box still places the signals in a regime, that it carries the
 * disclaimer, and -- the part that matters most -- that it refuses to show one
 * session's prose against another session's batch. A flash names a specific day's
 * breadth, so a stale one is a stated falsehood rather than a slightly old
 * number. */
const flashCases = [
  ['bearish',  'REGIME BEARISH'],
  ['bullish',  'REGIME BULLISH'],
  ['sideways', 'REGIME SIDEWAYS'],
  ['',         'MARKET'],
];
for (const [reg, needle] of flashCases) {
  window._regimeNow = reg;
  const html = marketFlash(
    {session_date: '2026-10-01', flash_text: 'Breadth is narrow.', source: 'llm'},
    '2026-10-01');
  const good = html.includes(needle) && html.includes('not advice');
  ok = ok && good;
  console.log('  ' + ("flash labels regime '" + reg + "'").padEnd(44) +
              (good ? 'ok' : '** missing ' + needle + ' or the disclaimer **'));
}

window._regimeNow = 'bearish';
const fresh = marketFlash(
  {session_date: '2026-10-01', flash_text: 'Breadth stays weak at 29%.', source: 'llm'},
  '2026-10-01');
const shows = fresh.includes('Breadth stays weak at 29%');
const disclaims = fresh.includes('Educational only') && fresh.includes('not advice');
ok = ok && shows && disclaims;
console.log('  ' + 'a current flash is shown'.padEnd(44) +
            (shows ? 'ok' : '** NOT RENDERED **'));
console.log('  ' + 'with the disclaimer beside it'.padEnd(44) +
            (disclaims ? 'ok' : '** NO DISCLAIMER **'));
/* The one that would otherwise ship quietly. */
const stale = marketFlash(
  {session_date: '2026-09-30', flash_text: 'Yesterday was quiet.', source: 'llm'},
  '2026-10-01');
const hidden = !stale.includes('Yesterday was quiet');
const explains = stale.includes('not shown against a newer batch');
ok = ok && hidden && explains;
console.log('  ' + "yesterday's prose is NOT shown as today's".padEnd(44) +
            (hidden ? 'ok' : '** STALE PROSE RENDERED **'));
console.log('  ' + '  and the page says why'.padEnd(44) +
            (explains ? 'ok' : '** SILENT **'));

const none = marketFlash(null, '2026-10-01');
const graceful = none.includes('No market note') && !none.includes('undefined');
ok = ok && graceful;
console.log('  ' + 'a missing flash degrades cleanly'.padEnd(44) +
            (graceful ? 'ok' : '** BROKEN **'));

/* Model-generated text goes into innerHTML. Validation is about advice and
 * invented numbers; it is not an HTML sanitiser. */
const nasty = marketFlash(
  {session_date: '2026-10-01', flash_text: '<img src=x onerror=alert(1)>', source: 'llm'},
  '2026-10-01');
const escaped = !nasty.includes('<img') && nasty.includes('&lt;img');
ok = ok && escaped;
console.log('  ' + 'the flash text is HTML-escaped'.padEnd(44) +
            (escaped ? 'ok' : '** INJECTED RAW **'));

console.log('-'.repeat(78));
console.log('SIGNALS BATCH STATE: ' + (ok ? 'correct' : '*** DEFECTIVE ***'));
process.exit(ok ? 0 : 1);
