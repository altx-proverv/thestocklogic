/* LIVE MEANS FRESH. Does the home page only say LIVE when the feed is?
 *
 * ON 4 NOVEMBER 2026 AT 00:18 IST THE PAGE SAID "Live · 00:18:55 IST" over the
 * 15:15 snapshot of the day before, tagged the sector grid LIVE, and wrote the
 * headline in the present tense: "DEFENCE and TELECOM are leading". It counted
 * any intraday_prices row as live, and that table is upserted by the detection
 * engine and never cleared -- so this happened every night, weekend and holiday.
 *
 * THE RULE, which this file exists to hold: LIVE is market open AND the newest
 * snapshot_at within LIVE_MAX_AGE_S. Open but older is STALE, with its age.
 * Not open is CLOSED, in the past tense, with the session named.
 *
 * Functions are EXTRACTED FROM THE PAGE, never copied, and run against a fake
 * clock and a stub DOM: resolvePrices, renderState, renderTags and
 * renderReading are the real ones. The threshold is read from the page too, so
 * the boundary checks follow the constant rather than a number typed here.
 *
 *   node tests/home_freshness.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'home.html'), 'utf8');

let fails = 0;
function check(name, cond, detail) {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}` +
              (cond || detail === undefined ? '' : `   got: ${detail}`));
  if (!cond) fails++;
}
function head(t) { console.log('\n' + t + '\n' + '-'.repeat(100)); }

function extract(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('home.html no longer defines ' + name +
                             ' -- the freshness logic has moved; repoint this test');
  let j = src.indexOf('{', i), depth = 0, k = j;
  for (; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (!depth) break; }
  }
  return src.slice(i, k + 1);
}
function extractVar(name) {
  const m = src.match(new RegExp('var ' + name + '=([^;]+);'));
  if (!m) throw new Error('home.html no longer defines var ' + name);
  return 'var ' + name + '=' + m[1] + ';';
}

const FNS = ['esc', 'istNow', 'istDate', 'hhmmss', 'dayName', 'marketOpen',
             'ageSeconds', 'snapFresh', 'feedState', 'fmtAge', 'istHhmm', 'istDay',
             'newestStamp', 'sessionTag', 'resolvePrices', 'renderState',
             'renderTags', 'changeFeedDead', 'renderReading'];
const CODE = [extractVar('LIVE_MAX_AGE_S'), extractVar('NO_PRICE')]
  .concat(FNS.map(extract)).join('\n');

/* A page at a given instant: the real functions, a fake clock, a stub DOM. */
function page(nowIso, data) {
  const NOW = Date.parse(nowIso);
  class FakeDate extends Date {
    constructor(...a) { if (a.length) super(...a); else super(NOW); }
    static now() { return NOW; }
  }
  const els = {};
  const document = { getElementById: (id) =>
    (els[id] = els[id] || { textContent: '', className: '', innerHTML: '' }) };
  const S = {
    prices: data.prices, idx: data.idx || [], orb: [], breadth: data.breadth || [],
    events: [], heavy: [], intraday: data.intraday, closes: data.closes,
    sectors: data.sectors, covered: 0, uncovered: 0,
    src: { prices: null, priceDate: null, priceSource: null, priceAsOf: null,
           ageS: null, tape: null, events: null, landed: null } };
  const ctx = vm.createContext({ Date: FakeDate, Math, String, isNaN, parseFloat,
                                 S, document });
  vm.runInContext(CODE, ctx);
  ctx.resolvePrices(); ctx.renderState(); ctx.renderTags(); ctx.renderReading();
  return { S, els, ctx };
}

/* 497 symbols, as the engine writes them; the universe a little larger. */
const N = 497;
const SYMS = Array.from({ length: 520 }, (_, i) => 'S' + i);
function intradayAt(iso, n = N) {
  return SYMS.slice(0, n).map((s, i) => ({ symbol: s, ltp: 100, prev_close: 99,
    change_pct: i % 3 ? 1.1 : -0.7, volume_ratio: 1, snapshot_at: iso }));
}
function closesOn(day) {
  return SYMS.map((s, i) => ({ symbol: s, session_date: day, close: 100,
    change_pct: i % 2 ? 0.5 : -0.4 }));
}
const prices = () => SYMS.map((s) => ({ symbol: s, ltp: 1, change_pct: 0,
                                       updated_at: '2026-11-03T09:45:04+00:00' }));
const sectors = [{ name: 'DEFENCE', pct: 2 }, { name: 'TELECOM', pct: 1.3 },
                 { name: 'METAL', pct: -0.4 }, { name: 'ENERGY', pct: -0.6 }];
const MAX = +extractVar('LIVE_MAX_AGE_S').match(/=(\d+)/)[1];

function scene(nowIso, snapIso, closeDay, extra = {}) {
  return page(nowIso, Object.assign({ prices: prices(),
    intraday: snapIso ? intradayAt(snapIso, extra.n) : [],
    closes: closesOn(closeDay), sectors }, extra));
}
const liveBadge = (p) => /tag-live|>LIVE</.test(p.els['sc-tag'].innerHTML) ||
                         p.els.dot.className === 'dot on' ||
                         /^Live/.test(p.els.state.textContent);

// ════════════════════════════════════════════════════════════════
head('1. THE NIGHT OF 3-4 NOV: MARKET CLOSED, 15:15 SNAPSHOT, CLOSES LANDED');
// ════════════════════════════════════════════════════════════════
let p = scene('2026-11-03T18:48:55Z' /* 00:18:55 IST Wed 4 Nov */,
              '2026-11-03T09:45:08Z', '2026-11-03');
check('state is CLOSED', p.S.src.prices === 'closed', p.S.src.prices);
check('no LIVE badge anywhere (tag, dot, header)', !liveBadge(p),
      `${p.els['sc-tag'].innerHTML} | ${p.els.dot.className} | ${p.els.state.textContent}`);
check('the header names the session', p.els.state.textContent ===
      'Market closed · showing the close of Tue 3 Nov', p.els.state.textContent);
check('the grid tag says CLOSE · Tue 3 Nov', /CLOSE &middot; Tue 3 Nov/.test(p.els['sc-tag'].innerHTML),
      p.els['sc-tag'].innerHTML);
check('the headline is past tense: "DEFENCE and TELECOM led"',
      / led; /.test(p.els.reading.textContent) && p.els.reading.textContent.startsWith('At the close on Tue 3 Nov'),
      p.els.reading.textContent);
check('  and never present tense', !/are leading|are green|are under pressure/.test(p.els.reading.textContent),
      p.els.reading.textContent);
check('the grid shows the CLOSE, which beats a same-day snapshot',
      p.S.src.priceSource === 'close', p.S.src.priceSource);

// ════════════════════════════════════════════════════════════════
head('2. 16:00 IST, BEFORE THE EVENING CHAIN: CLOSED, BUT THE SNAPSHOT IS NEWER');
// ════════════════════════════════════════════════════════════════
p = scene('2026-11-03T10:30:00Z', '2026-11-03T09:45:08Z', '2026-11-02');
check('state is CLOSED', p.S.src.prices === 'closed', p.S.src.prices);
check('no LIVE badge', !liveBadge(p), p.els.state.textContent);
check('the snapshot is said as a time, not as "the close"',
      /AS OF 15:15 &middot; Tue 3 Nov/.test(p.els['sc-tag'].innerHTML), p.els['sc-tag'].innerHTML);
check('the header says prices as of 15:15 IST, Tue 3 Nov',
      p.els.state.textContent === 'Market closed · showing prices as of 15:15 IST, Tue 3 Nov',
      p.els.state.textContent);
check('the headline is past tense and timed',
      p.els.reading.textContent.startsWith('As of 15:15 IST on Tue 3 Nov') &&
      / were leading; /.test(p.els.reading.textContent), p.els.reading.textContent);

// ════════════════════════════════════════════════════════════════
head('3. 11:02 IST, SNAPSHOT 2 MINUTES OLD: LIVE');
// ════════════════════════════════════════════════════════════════
p = scene('2026-11-03T05:32:00Z', '2026-11-03T05:30:00Z', '2026-11-02');
check('state is LIVE', p.S.src.prices === 'live', p.S.src.prices);
check('the grid tag is LIVE', /tag-live/.test(p.els['sc-tag'].innerHTML), p.els['sc-tag'].innerHTML);
check('the dot is on', p.els.dot.className === 'dot on', p.els.dot.className);
check('the header carries the FEED\'s time, not the reader\'s clock',
      p.els.state.textContent === 'Live · prices as of 11:00 IST', p.els.state.textContent);
check('the headline is present tense', / are leading; /.test(p.els.reading.textContent),
      p.els.reading.textContent);

// ════════════════════════════════════════════════════════════════
head('4. 11:14 IST, SNAPSHOT 14 MINUTES OLD: STALE, AMBER, WITH THE REAL AGE');
// ════════════════════════════════════════════════════════════════
p = scene('2026-11-03T05:44:00Z', '2026-11-03T05:30:00Z', '2026-11-02');
check('state is STALE', p.S.src.prices === 'stale', p.S.src.prices);
check('no LIVE badge', !liveBadge(p), `${p.els['sc-tag'].innerHTML} | ${p.els.state.textContent}`);
check('the grid tag reads STALE · 14m', /tag-stale">STALE &middot; 14m</.test(p.els['sc-tag'].innerHTML),
      p.els['sc-tag'].innerHTML);
check('the dot is amber', p.els.dot.className === 'dot stale', p.els.dot.className);
check('the header gives the age and the as-of time',
      p.els.state.textContent === 'STALE · 14m · prices as of 11:00 IST', p.els.state.textContent);
check('the headline is past tense', / were leading; /.test(p.els.reading.textContent) &&
      !/are leading/.test(p.els.reading.textContent), p.els.reading.textContent);

// ════════════════════════════════════════════════════════════════
head('5. THE BOUNDARY IS THE PAGE\'S OWN CONSTANT, LIVE_MAX_AGE_S = ' + MAX + 's');
// ════════════════════════════════════════════════════════════════
const snap = Date.parse('2026-11-03T05:30:00Z');
const at = (s) => new Date(snap + s * 1000).toISOString();
p = scene(at(MAX), '2026-11-03T05:30:00Z', '2026-11-02');
check(`age = ${MAX}s is LIVE`, p.S.src.prices === 'live', p.S.src.prices);
p = scene(at(MAX + 1), '2026-11-03T05:30:00Z', '2026-11-02');
check(`age = ${MAX + 1}s is STALE`, p.S.src.prices === 'stale', p.S.src.prices);
check('the threshold clears the worst observed write gap (311s) and is under two cycles (600s)',
      MAX >= 311 && MAX < 600, MAX);

// ════════════════════════════════════════════════════════════════
head('6. THE OTHER WAYS A ROW CAN BE OLD');
// ════════════════════════════════════════════════════════════════
/* Inputs on whole minutes so the label follows from them: 09:45:00 to 03:45:00
   the next day is exactly 18h 0m. */
p = scene('2026-11-03T03:45:00Z' /* 09:15 IST, before the first write */,
          '2026-11-02T09:45:00Z', '2026-11-02');
check('09:15 with yesterday\'s snapshot: STALE, not LIVE', p.S.src.prices === 'stale', p.S.src.prices);
check('  and it says how old: 18h', /STALE &middot; 18h</.test(p.els['sc-tag'].innerHTML),
      p.els['sc-tag'].innerHTML);
p = scene('2026-11-07T06:00:00Z' /* Saturday */, '2026-11-06T09:45:08Z', '2026-11-06');
check('a Saturday is CLOSED', p.S.src.prices === 'closed' && !liveBadge(p), p.S.src.prices);
p = scene('2026-11-03T05:32:00Z', '2026-11-03T05:30:00Z', '2026-11-02', { n: 30 });
check('fresh but only 30 rows: STALE, the feed is not carrying the market',
      p.S.src.prices === 'stale', p.S.src.prices);
p = scene('2026-11-03T05:32:00Z', null, '2026-11-02');
check('open with no intraday rows at all: STALE · no feed', p.S.src.prices === 'stale' &&
      /STALE &middot; no feed/.test(p.els['sc-tag'].innerHTML), p.els['sc-tag'].innerHTML);
// One symbol the engine stopped writing keeps an old row; it must not count.
const mixed = intradayAt('2026-11-03T05:30:00Z');
mixed[0].snapshot_at = '2026-11-02T09:45:08Z';
p = page('2026-11-03T05:32:00Z', { prices: prices(), intraday: mixed,
         closes: closesOn('2026-11-02'), sectors });
check('a row is live on its OWN snapshot_at: a stale row is not live',
      p.S.prices[0].live === false && p.S.prices[1].live === true,
      `${p.S.prices[0].live}/${p.S.prices[1].live}`);

// ════════════════════════════════════════════════════════════════
head('7. THE INDEX STRIP AND BREADTH PANEL BY THE SAME RULE');
// ════════════════════════════════════════════════════════════════
p = scene('2026-11-03T07:30:00Z' /* 13:00 IST */, '2026-11-03T07:30:00Z', '2026-11-02',
          /* 06:00:00 to 07:30:00 is exactly 1h 30m */
          { idx: [{ symbol: 'NIFTY50', updated_at: '2026-11-03T06:00:00+00:00' }] });
check('index_quotes 90 minutes old in session: STALE, not LIVE',
      /tag-stale">STALE &middot; 1h 30m</.test(p.els['ix-tag'].innerHTML), p.els['ix-tag'].innerHTML);
p = scene('2026-11-03T18:48:55Z', '2026-11-03T09:45:08Z', '2026-11-03',
          { idx: [{ symbol: 'NIFTY50', updated_at: '2026-11-03T09:45:03+00:00' }],
            breadth: [{ session_date: '2026-11-03', at: '2026-11-03T09:45:09+00:00' }] });
check('closed: the index strip says AS OF 15:15, not LIVE',
      /AS OF 15:15 &middot; Tue 3 Nov/.test(p.els['ix-tag'].innerHTML), p.els['ix-tag'].innerHTML);
check('closed: the breadth panel says AS OF 15:15 · Tue 3 Nov',
      /AS OF 15:15 &middot; Tue 3 Nov/.test(p.els['bd-tag'].innerHTML), p.els['bd-tag'].innerHTML);
const code = src.replace(/\/\*[\s\S]*?\*\//g, ' ');
check('breadth is fetched NEWEST first', /breadth_snapshots\?[^']*order=at\.desc/.test(code) &&
      !/breadth_snapshots\?[^']*order=at\.asc/.test(code));
check('  and narrowed to one session before it is drawn',
      /S\.breadth=res\[3\]\.filter\(/.test(code));

// ════════════════════════════════════════════════════════════════
head('8. EVERY ROUTE TO LIVE GOES THROUGH THE FRESHNESS CHECK');
// ════════════════════════════════════════════════════════════════
const fs_ = extract('feedState').replace(/\/\*[\s\S]*?\*\//g, '');
check('feedState asks snapFresh', /snapFresh\(/.test(fs_));
check('snapFresh compares the age to LIVE_MAX_AGE_S',
      /LIVE_MAX_AGE_S/.test(extract('snapFresh')));
const rp = extract('resolvePrices').replace(/\/\*[\s\S]*?\*\//g, '');
check('resolvePrices takes its state from feedState', /state=feedState\(/.test(rp));
check('  and assigns no state literal of its own', !/state\s*=\s*'live'/.test(rp));
check('the index tag takes its state from feedState',
      /ixSt=feedState\(/.test(extract('renderTags')));
check('the old clock-only index tag is gone',
      !/sessionTag\(marketOpen\(\)\?'live'/.test(code));

console.log('\n' + '='.repeat(100));
if (fails) { console.log(`FAILED -- ${fails} check(s)`); process.exit(1); }
console.log('PASSED -- LIVE is shown only when the market is open and the snapshot is fresh');
