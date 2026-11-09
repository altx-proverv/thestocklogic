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
             'heavyState', 'renderTags', 'changeFeedDead', 'renderReading',
             'renderHeavy', 'flashText', 'renderFlash', 'num', 'pct', 'sign',
             'priceUnchangedS', 'priceFrozen', 'renderIdx'];
const CODE = [extractVar('LIVE_MAX_AGE_S'), extractVar('NO_PRICE'),
              extractVar('VALUE_MAX_UNCHANGED_MIN'), extractVar('VALUE_CHECK_LAST_MIN'),
              extractVar('IDX_NAME'), extractVar('MIN_RANKED_MEMBERS')]
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
    events: [], heavy: data.heavy || [], intraday: data.intraday, closes: data.closes,
    flash: data.flash || [],
    sectors: data.sectors, covered: 0, uncovered: 0,
    src: { prices: null, priceDate: null, priceSource: null, priceAsOf: null,
           ageS: null, tape: null, events: null, landed: null } };
  const ctx = vm.createContext({ Date: FakeDate, Math, String, isNaN, parseFloat,
                                 S, document, Object,
                                 /* only counted by renderHeavy; the real map is the
                                    universe, asserted by tests/test_home_page.py */
                                 SECTOR_MAP: { HV1: 'X' } });
  vm.runInContext(CODE, ctx);
  ctx.resolvePrices(); ctx.renderState(); ctx.renderTags(); ctx.renderReading();
  ctx.renderHeavy(); ctx.renderFlash(); ctx.renderIdx();
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

// ════════════════════════════════════════════════════════════════
head('9. THE HEAVY-VOLUME PANEL: THE SAME THREE STATES, ON THE SWEEP\'S CLOCK');
// ════════════════════════════════════════════════════════════════
const hvRow = (iso, day) => ({ session_date: day, symbol: 'HV1', snapshot_at: iso,
                               volume_ratio: 4, change_pct: 0.2, ltp: 10, tracked: 497 });
p = scene('2026-11-03T18:48:55Z' /* 00:18:55 IST Wed 4 Nov */, '2026-11-03T09:45:08Z',
          '2026-11-03', { heavy: [hvRow('2026-11-03T09:45:08+00:00', '2026-11-03')] });
check('post-close: AS OF 15:15 · Tue 3 Nov, not LIVE, not a bare CLOSE',
      /AS OF 15:15 &middot; Tue 3 Nov/.test(p.els['hv-tag'].innerHTML), p.els['hv-tag'].innerHTML);
p = scene('2026-11-03T05:32:00Z', '2026-11-03T05:30:00Z', '2026-11-02',
          { heavy: [hvRow('2026-11-03T05:30:01+00:00', '2026-11-03')] });
check('live sweep that found the set: LIVE', /tag-live/.test(p.els['hv-tag'].innerHTML),
      p.els['hv-tag'].innerHTML);
check('  and the set is current', p.ctx.heavyState().current === true);
p = scene('2026-11-03T05:44:00Z', '2026-11-03T05:30:00Z', '2026-11-02',
          { heavy: [hvRow('2026-11-03T05:30:01+00:00', '2026-11-03')] });
check('sweep 14 minutes old in session: STALE · 14m',
      /tag-stale">STALE &middot; 14m</.test(p.els['hv-tag'].innerHTML), p.els['hv-tag'].innerHTML);
/* 14:40 set, sweeps since have found nothing: live, but that set is not "now". */
p = scene('2026-11-03T09:42:00Z', '2026-11-03T09:40:00Z', '2026-11-02',
          { heavy: [hvRow('2026-11-03T09:10:00+00:00', '2026-11-03')] });
check('a 14:40 set under a 15:10 sweep is not current', p.ctx.heavyState().current === false);
check('  so the panel says nothing qualifies right now, not the old set',
      /Nothing is trading on unusual volume without moving right now/.test(p.els.heavy.innerHTML),
      p.els.heavy.innerHTML);
p = scene('2026-11-03T03:50:00Z' /* 09:20 IST */, '2026-11-03T03:50:00Z', '2026-11-02',
          { heavy: [hvRow('2026-11-02T09:45:08+00:00', '2026-11-02')] });
check('09:20 with yesterday\'s set: it starts filling at 09:45, not yesterday as now',
      /starts filling at 09:45/.test(p.els.heavy.innerHTML), p.els.heavy.innerHTML);
const hvStmt = (extract('renderTags').replace(/\/\*[\s\S]*?\*\//g, '')
                .match(/set\('hv-tag'[\s\S]*?\);/) || [''])[0];
check('the heavy tag takes its state from heavyState, never a literal',
      /sessionTag\(hv\.state/.test(hvStmt) && !/sessionTag\('/.test(hvStmt), hvStmt);

// ════════════════════════════════════════════════════════════════
head('10. THE FLASH LINE NAMES ITS SESSION');
// ════════════════════════════════════════════════════════════════
const FL = 'Breadth held at 59.7% advancing. ATLAS is watching 450 setups, though no entries triggered today.';
p = scene('2026-11-03T18:48:55Z', '2026-11-03T09:45:08Z', '2026-11-03',
          { flash: [{ session_date: '2026-11-03 00:00:00+00', generated_at: '2026-11-03T13:20:31+00:00',
                      flash_text: FL }] });
const shown = p.els.flashP.textContent;
check('after midnight it leads with its session', shown.startsWith('Tue 3 Nov · '), shown);
check('  and "today" became the session it meant', / triggered on Tue 3 Nov\.$/.test(shown), shown);
check('  so it makes no present-tense claim about today', !/\btoday\b/i.test(shown), shown);
const fx = (text, gen) => p.ctx.flashText({ session_date: '2026-11-03', generated_at: gen, flash_text: text });
check('"Today ..." at a sentence start keeps its capital',
      fx('Today breadth held.', '2026-11-03T13:20:31Z') === 'Tue 3 Nov · On Tue 3 Nov breadth held.',
      fx('Today breadth held.', '2026-11-03T13:20:31Z'));
check('"today\'s" becomes the session\'s',
      fx("today's move", '2026-11-03T13:20:31Z') === "Tue 3 Nov · Tue 3 Nov's move",
      fx("today's move", '2026-11-03T13:20:31Z'));
check('generated on a later day: "today" is NOT rewritten, only labelled',
      fx('no entries today.', '2026-11-03T19:30:00Z') === 'Tue 3 Nov · no entries today.',
      fx('no entries today.', '2026-11-03T19:30:00Z'));
check('text with no "today" is labelled and otherwise untouched',
      fx('Breadth held.', '2026-11-03T13:20:31Z') === 'Tue 3 Nov · Breadth held.',
      fx('Breadth held.', '2026-11-03T13:20:31Z'));
check('a word containing it is left alone',
      fx('todays and yesterday-today', '2026-11-03T13:20:31Z').indexOf('todays') >= 0);

// ════════════════════════════════════════════════════════════════
head('11. A FRESH ROW WITH A FROZEN PRICE IS NOT LIVE');
// ════════════════════════════════════════════════════════════════
/* The thresholds are the page's own, read from it -- never typed here. */
const NMAP = vm.runInNewContext(extractVar('VALUE_MAX_UNCHANGED_MIN') + ' VALUE_MAX_UNCHANGED_MIN');
const LAST = +extractVar('VALUE_CHECK_LAST_MIN').match(/=(\d+)/)[1];
const NOW11 = '2026-11-03T05:30:00Z';                      /* 11:00 IST, session */
const ago = (base, s) => new Date(Date.parse(base) - s * 1000).toISOString();
function idxRow(sym, changedAgoS, base = NOW11) {
  return { symbol: sym, display_name: sym, ltp: 100, prev_close: 99, change_pct: 1.01,
           is_future: sym === 'USDINR', expiry: null,
           updated_at: ago(base, 20),                        /* the row: 20s old, fresh */
           price_changed_at: changedAgoS === null ? null : ago(base, changedAgoS) };
}
const strip = (now, rows) => scene(now, ago(now, 60), '2026-11-02', { idx: rows });
const tileOf = (p, sym) => (p.els.idx.innerHTML.split('<div class="idx-t').find(
  (t) => t.indexOf('>' + sym + '<') >= 0) || '');

const n50 = NMAP.NIFTY50, nUsd = NMAP.USDINR;
p = strip(NOW11, [idxRow('NIFTY50', n50 * 60 - 1)]);
check(`NIFTY 50 unchanged ${n50 * 60 - 1}s (under N=${n50} min): LIVE, tile not stale`,
      /tag-live/.test(p.els['ix-tag'].innerHTML) && !/stale/.test(tileOf(p, 'NIFTY50').slice(0, 20)),
      p.els['ix-tag'].innerHTML);
p = strip(NOW11, [idxRow('NIFTY50', n50 * 60)]);
check(`NIFTY 50 unchanged ${n50} min under a 20s-old row: NOT live`,
      /tag-stale/.test(p.els['ix-tag'].innerHTML), p.els['ix-tag'].innerHTML);
check('  the tile is marked stale and says how long',
      /^ stale"/.test(tileOf(p, 'NIFTY50')) && /PRICE UNCHANGED 4M/.test(tileOf(p, 'NIFTY50')),
      tileOf(p, 'NIFTY50').slice(0, 160));
p = strip(NOW11, [idxRow('USDINR', 7 * 86400)]);
check('USDINR: fresh row, price a week old (the weekly-contract defect): NOT live',
      /tag-stale/.test(p.els['ix-tag'].innerHTML) && /^ stale"/.test(tileOf(p, 'USDINR')),
      p.els['ix-tag'].innerHTML);
p = strip(NOW11, [idxRow('USDINR', (nUsd - 1) * 60)]);
check(`USDINR unchanged ${nUsd - 1} min (its measured N is ${nUsd}): still live`,
      /tag-live/.test(p.els['ix-tag'].innerHTML), p.els['ix-tag'].innerHTML);
p = strip(NOW11, [idxRow('NIFTY50', 30), idxRow('USDINR', nUsd * 60)]);
check('one frozen tile makes the strip STALE even when the rest move',
      /tag-stale/.test(p.els['ix-tag'].innerHTML) && !/^ stale"/.test(tileOf(p, 'NIFTY50')),
      p.els['ix-tag'].innerHTML);
p = strip(NOW11, [idxRow('NIFTY50', null)]);
check('a price never seen to change is not evidence of live',
      /tag-stale/.test(p.els['ix-tag'].innerHTML) && /NO PRICE CHANGE SEEN YET/.test(tileOf(p, 'NIFTY50')),
      tileOf(p, 'NIFTY50').slice(0, 160));
/* LAST + 6 minutes IST, i.e. 15:20 on 3 Nov; IST is UTC + 330 minutes. */
const after = new Date(Date.parse('2026-11-03T00:00:00Z') + ((LAST + 6) - 330) * 60000).toISOString();
p = strip(after, [idxRow('NIFTY50', 6 * 60, after)]);
check(`after ${Math.floor(LAST / 60)}:${LAST % 60} the closing hold is not called stale (no daily false alarm)`,
      /tag-live/.test(p.els['ix-tag'].innerHTML), `${after} ${p.els['ix-tag'].innerHTML}`);
p = strip('2026-11-03T18:48:55Z', [idxRow('NIFTY50', 3 * 3600, '2026-11-03T18:48:55Z')]);
check('market closed: the value check does not apply',
      !/^ stale"/.test(tileOf(p, 'NIFTY50')), tileOf(p, 'NIFTY50').slice(0, 60));
/* THE EVIDENCE, NOT THE PAGE'S OWN NUMBER. The boundary checks above read N
   from the page, so a loosened N would move them with it. This pins each N to
   the measurement it came from: longest run of identical one-minute values
   before 15:15, 6 Oct - 3 Nov 2026 (Upstox one-minute candles, carried forward
   through minutes with no trade), plus the margin of 3 that atlas/freshness.py
   took. Changing an N means changing this evidence, on purpose. */
const MEASURED_MAX = { NIFTY50: 1, NIFTYBANK: 1, NIFTYFIN: 1, NIFTYMIDCAP: 1,
                       NIFTYSMLCAP: 1, INDIAVIX: 8, USDINR: 41 };
const MARGIN = 3;
Object.keys(MEASURED_MAX).forEach((k) => check(
  `${k}: N = measured max ${MEASURED_MAX[k]} + ${MARGIN}`, NMAP[k] === MEASURED_MAX[k] + MARGIN,
  NMAP[k]));
check('every instrument on the strip has a measured N',
      ['NIFTY50', 'NIFTYBANK', 'NIFTYFIN', 'NIFTYMIDCAP', 'NIFTYSMLCAP', 'INDIAVIX', 'USDINR']
        .every((k) => typeof NMAP[k] === 'number'), JSON.stringify(NMAP));

// ════════════════════════════════════════════════════════════════
head('12. THE USDINR LABEL COMES FROM THE CONTRACT, NOT FROM A TYPED WORD');
// ════════════════════════════════════════════════════════════════
const usdRow = (inst) => ({ symbol: 'USDINR', display_name: 'USDINR FUT', instrument: inst,
  ltp: 96.21, change_pct: 0, is_future: true, expiry: '2026-11-06',
  updated_at: '2026-11-03T09:45:03+00:00', price_changed_at: null });
p = scene('2026-11-03T18:48:55Z', '2026-11-03T09:45:08Z', '2026-11-03', { idx: [usdRow('USDINR26N06FUT')] });
check('a weekly contract is labelled weekly', /weekly future/.test(tileOf(p, 'USDINR FUT')) &&
      !/monthly/.test(tileOf(p, 'USDINR FUT')), tileOf(p, 'USDINR FUT'));
p = scene('2026-11-03T18:48:55Z', '2026-11-03T09:45:08Z', '2026-11-03', { idx: [usdRow('USDINR26NOVFUT')] });
check('the monthly is labelled monthly', /monthly future/.test(tileOf(p, 'USDINR FUT')),
      tileOf(p, 'USDINR FUT'));
check('a tile with no row shows the exchange name, not the internal key',
      /NIFTY FIN SERVICE/.test(p.els.idx.innerHTML) && !/>NIFTYFIN</.test(p.els.idx.innerHTML));

console.log('\n' + '='.repeat(100));
if (fails) { console.log(`FAILED -- ${fails} check(s)`); process.exit(1); }
console.log('PASSED -- LIVE is shown only when the market is open and the snapshot is fresh');
