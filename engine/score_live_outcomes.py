"""
THE STOCK LOGIC — Live Signal Outcome Scorer (ORB / RBE) — DRY RUN
Scores live_signals using DAILY high/low as proxy for SL/T1 hits.
AMBIGUOUS when both hit same day. Read-only analysis.
"""
import os, sys, glob, logging, argparse
from collections import Counter
from pathlib import Path
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)
SUPABASE_URL = 'https://eibdlcanpudjgmkjxrga.supabase.co'
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY','')
STOCKS_DIR = Path('data/processed/stocks')
MAX_HOLD_DAYS = 5

def _h(): return {'apikey':SUPABASE_KEY,'Authorization':f'Bearer {SUPABASE_KEY}'}

def fetch():
    r=requests.get(f'{SUPABASE_URL}/rest/v1/live_signals?select=*&order=signal_date.asc',headers=_h())
    return r.json() if r.status_code==200 else []

def load(sym):
    f=STOCKS_DIR/f'{sym}.parquet'
    if not f.exists(): return None
    df=pd.read_parquet(f); df['date']=pd.to_datetime(df['date'])
    return df.sort_values('date').reset_index(drop=True)

def score(sig,df):
    d=(sig.get('direction') or '').upper()
    entry=float(sig.get('entry') or 0); sl=float(sig.get('sl') or 0); t1=float(sig.get('target_1') or 0)
    if not entry or not sl or not t1: return 'NO_LEVELS',None
    try: sd=pd.to_datetime(sig['signal_date'])
    except: return 'NO_DATE',None
    fut=df[df['date']>=sd].head(MAX_HOLD_DAYS)
    if fut.empty: return 'NO_DATA',None
    for _,row in fut.iterrows():
        hi,lo=row['high'],row['low']; day=row['date'].date().isoformat()
        if d=='LONG': ht,hs = hi>=t1, lo<=sl
        else: ht,hs = lo<=t1, hi>=sl
        if ht and hs: return 'AMBIGUOUS',day
        if ht: return 'WIN_T1',day
        if hs: return 'LOSS',day
    return 'OPEN',None

def main():
    sigs=fetch(); log.info(f'Fetched {len(sigs)} live_signals')
    cache={}; results=[]
    for sig in sigs:
        sym=sig['symbol']
        if sym not in cache: cache[sym]=load(sym)
        df=cache[sym]
        if df is None: results.append((sig,'NO_CANDLES',None)); continue
        oc,ed=score(sig,df); results.append((sig,oc,ed))
    tally=Counter(r[1] for r in results)
    log.info(f'TALLY: {dict(tally)}')
    w=tally.get('WIN_T1',0); l=tally.get('LOSS',0); dec=w+l
    if dec: log.info(f'DECIDED {dec} | WIN {w} LOSS {l} | WIN RATE {round(w/dec*100)}%')
    log.info(f'AMBIGUOUS {tally.get("AMBIGUOUS",0)} | OPEN {tally.get("OPEN",0)}')
    bs={}
    for sig,oc,ed in results:
        s=sig.get('session','?'); bs.setdefault(s,Counter())[oc]+=1
    for s,c in bs.items():
        w=c.get('WIN_T1',0); l=c.get('LOSS',0); d=w+l
        log.info(f'  session={s}: WIN {w} LOSS {l} WR {round(w/d*100) if d else 0}% | AMB {c.get("AMBIGUOUS",0)} OPEN {c.get("OPEN",0)}')
    log.info('Sample:')
    for sig,oc,ed in results[:12]:
        log.info(f'  {sig["symbol"][:10]:10} {sig["direction"][:1]} {oc:10} {ed}')

if __name__=='__main__':
    main()
