/* Does the page know when the market is shut?
 *
 * ON MONDAY 5 OCTOBER 2026 THE PAGE LIED, THREE WAYS AT ONCE. Friday 2 October
 * was an NSE holiday, so Thursday 1 October's batch was correctly the current one
 * for Monday. The page said "NO SIGNALS for Fri, 2 Oct", labelled a valid batch
 * stale, and zeroed its own long/short counter to agree with itself.
 *
 * The cause was one line: the session stepper skipped weekends only, and the
 * holiday list lives in engine/trading_calendar.py which nothing served to the
 * browser. tools/stamp_config.py now writes it into a span.
 *
 * THE RULE, which this file exists to hold: trading days, never calendar days. A
 * batch carries to the next trading session however many non-trading days sit
 * between, and "no signals" must never show while a valid batch exists for it.
 *
 * Functions are EXTRACTED FROM THE PAGE, never copied. A copy passes forever
 * while the page drifts, which is exactly how this area keeps failing.
 *
 *   node tests/signals_holidays.js
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'signals.html'), 'utf8');

let fails = 0;
function check(name, cond, detail) {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}` +
              (cond || detail === undefined ? '' : `   ${detail}`));
  if (!cond) fails++;
}

function extract(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('signals.html no longer defines ' + name +
                             ' — the holiday-aware session logic has moved');
  let j = src.indexOf('{', i), depth = 0, k = j;
  for (; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (!depth) break; }
  }
  return src.slice(i, k + 1);
}

/* THE LIST COMES FROM THE PAGE'S OWN STAMPED SPAN, so this test also proves the
   span exists and is populated. Reading it from trading_calendar.py instead would
   test the calendar and not the page. */
const spanRe = /<span[^>]*id="nseHolidays"[^>]*>(?:<!--stamp:nse_holidays-->)?([^<]*)/;
const m = src.match(spanRe);
check('the page carries a stamped nseHolidays span', !!m);
const RAW = m ? m[1].trim() : '';
check('the span is populated', /\d{4}-\d{2}-\d{2}/.test(RAW), JSON.stringify(RAW.slice(0, 40)));
const COV = (src.match(/id="holidayCoverageTo"[^>]*>(?:<!--stamp:holiday_coverage_to-->)?(\d{4})/) || [])[1];
check('the page carries a coverage year', !!COV, String(COV));

/* 2 October 2026 is the whole point of this file. */
check('2026-10-02 is in the stamped list', RAW.indexOf('2026-10-02') >= 0);
check('the Diwali cluster is too', RAW.indexOf('2026-10-21') >= 0 && RAW.indexOf('2026-10-22') >= 0);

global.document = {
  getElementById: (id) => ({
    textContent: id === 'nseHolidays' ? RAW
               : id === 'holidayCoverageTo' ? String(COV || '') : ''
  })
};
/* the two IIFEs that read the spans, pulled by their var names */
function extractVar(name) {
  const i = src.indexOf('var ' + name + ' = (function()');
  if (i < 0) throw new Error('signals.html no longer defines ' + name);
  let j = src.indexOf('{', i), depth = 0, k = j;
  for (; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (!depth) break; }
  }
  const end = src.indexOf(';', k);
  return src.slice(i, end + 1);
}
eval(extractVar('HOLIDAYS').replace(/^var /, 'global.'));
eval(extractVar('HOLIDAY_COVERAGE_TO').replace(/^var /, 'global.'));
eval(extract('isTradingDay'));
eval(extract('nextTD'));
eval(extract('tradingDayOrNext'));

console.log('\n── the calendar reached the browser ──');
check('holidays parsed', HOLIDAYS.n >= 10, String(HOLIDAYS.n));
check('coverage year parsed', HOLIDAY_COVERAGE_TO >= 2026, String(HOLIDAY_COVERAGE_TO));

console.log('\n── trading days ──');
check('Thu 2026-10-01 is a trading day', isTradingDay('2026-10-01'));
check('Fri 2026-10-02 is NOT (Gandhi Jayanti)', !isTradingDay('2026-10-02'));
check('Sat 2026-10-03 is NOT', !isTradingDay('2026-10-03'));
check('Sun 2026-10-04 is NOT', !isTradingDay('2026-10-04'));
check('Mon 2026-10-05 is a trading day', isTradingDay('2026-10-05'));

console.log('\n── the batch carries across the closure ──');
check('nextTD(2026-10-01) === 2026-10-05, not 10-02',
      nextTD('2026-10-01') === '2026-10-05', nextTD('2026-10-01'));
check('nextTD steps the Diwali cluster whole',
      nextTD('2026-10-20') === '2026-10-23', nextTD('2026-10-20'));
check('a plain Friday still lands on Monday',
      nextTD('2026-10-09') === '2026-10-12', nextTD('2026-10-09'));
check('tradingDayOrNext leaves a trading day alone',
      tradingDayOrNext('2026-10-05') === '2026-10-05');
check('tradingDayOrNext moves a holiday forward',
      tradingDayOrNext('2026-10-02') === '2026-10-05', tradingDayOrNext('2026-10-02'));

console.log('\n── THE REGRESSION: Monday 5 October, the batch is NOT stale ──');
/* THE THRESHOLD IS READ OUT OF THE PAGE, not restated here. The first version of
   this helper hardcoded 8 while the page used 6, so the test was measuring a rule
   the page does not implement -- which is the same defect as copying a function
   instead of extracting it, one level down. */
const GAP_LIMIT = (() => {
  const m = src.match(/_gapDays\s*>\s*(\d+)/);
  if (!m) throw new Error('signals.html no longer has a _gapDays threshold');
  return parseInt(m[1], 10);
})();
function stale(batchDate, today, canKnow) {
  const sessionDate = nextTD(batchDate);
  const expected = tradingDayOrNext(today);
  if (canKnow === false) {
    const gap = Math.round((Date.parse(expected) - Date.parse(sessionDate)) / 86400000);
    return { stale: gap > GAP_LIMIT, sessionDate, expected, gap };
  }
  return { stale: sessionDate < expected, sessionDate, expected };
}
let r = stale('2026-10-01', '2026-10-05', true);
check('Thu batch on Mon 5 Oct is CURRENT', r.stale === false,
      `session ${r.sessionDate} vs expected ${r.expected}`);
check('  and it is labelled for Mon 5 Oct', r.sessionDate === '2026-10-05', r.sessionDate);

console.log('\n── but a genuinely old batch still trips ──');
r = stale('2026-09-25', '2026-10-05', true);
check('25 Sep batch on 5 Oct is STALE', r.stale === true,
      `session ${r.sessionDate} vs expected ${r.expected}`);
/* 19 Oct is a Monday whose next session is Tue 20 Oct, so by Friday the 23rd it
   genuinely IS stale -- the first version of this case asserted otherwise and was
   simply wrong about the dates. The batch that straddles the closure is Tuesday
   the 20th, whose next session is Friday the 23rd. */
r = stale('2026-10-19', '2026-10-23', true);
check('a Mon batch IS stale by Fri across Diwali', r.stale === true,
      `session ${r.sessionDate} vs expected ${r.expected}`);
r = stale('2026-10-20', '2026-10-23', true);
check('the Tue batch straddling Diwali is CURRENT', r.stale === false,
      `session ${r.sessionDate} vs expected ${r.expected}`);
check('  and it is labelled for Fri 23 Oct', r.sessionDate === '2026-10-23',
      r.sessionDate);

console.log('\n── the fallback, when the list is missing or expired ──');
/* Neither extreme is acceptable: suppressing staleness renders an old batch as
   today's trades, and weekend-only stepping reproduces the false NO SIGNALS. The
   fallback claims staleness only on a gap no closure explains. */
r = stale('2026-10-01', '2026-10-05', false);
check('one holiday does NOT trip the fallback', r.stale === false, `gap ${r.gap}d`);
r = stale('2026-10-19', '2026-10-23', false);
check('the Diwali cluster does not either', r.stale === false, `gap ${r.gap}d`);
r = stale('2026-09-25', '2026-10-05', false);
check('the real 25 Sep outage still does', r.stale === true, `gap ${r.gap}d`);
/* The threshold is 6, not 8. The real outage is a SEVEN day gap, so the first
   version of this -- chosen from the length of a closure rather than measured
   against the case it has to catch -- would never have fired at all. */
check('the threshold separates 3-day clusters from the 7-day outage',
      GAP_LIMIT >= 4 && GAP_LIMIT <= 6, `GAP_LIMIT=${GAP_LIMIT}`);
r = stale('2026-10-19', '2026-10-23', false);
check('a 3-day cluster gap does not trip it', r.stale === false, `gap ${r.gap}d`);

console.log('\n── it cannot spin on a bad stamp ──');
check('nextTD is bounded', /for\s*\(\s*var\s+i\s*=\s*0\s*;\s*i\s*<\s*12\s*;/.test(extract('nextTD')));
check('an empty list warns in the console',
      src.indexOf('NSE holiday list is empty') >= 0);
check('weekdayOrNext is gone entirely', src.indexOf('weekdayOrNext') < 0);

console.log();
if (fails) { console.log(`FAILED ${fails}`); process.exit(1); }
console.log('All holiday checks passed.');
