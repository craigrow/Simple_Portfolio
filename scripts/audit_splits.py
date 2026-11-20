"""Audit copied portfolio stores against the ledger and reviewed issuer events.

Usage: python scripts/audit_splits.py PORTFOLIOS_DIRECTORY OUTPUT_DIRECTORY --as-of YYYY-MM-DD [--fetch]
Never modifies the input directory. Reports do not imply proof that no unreported event exists.
"""
import argparse
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import os
import shutil
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def quantities(tx, events, through):
    """Independent share roll-forward for this app's whole-lot sales contract."""
    open_lots = {}
    for row in tx.sort_values('DATE', kind='stable').itertuples():
        if row.DATE > through:
            continue
        if row.ACTION == 'BUY':
            open_lots[row.TRANSACTION_ID] = row
        elif row.ACTION == 'SELL':
            if row.LOT_ID not in open_lots:
                raise ValueError(f'Unknown or already closed sale lot: {row.LOT_ID}')
            del open_lots[row.LOT_ID]
        else:
            raise ValueError(f'Unsupported ledger action {row.ACTION}')
    result = defaultdict(Decimal)
    for row in open_lots.values():
        shares = Decimal(row.SHARES)
        for event in events:
            if event['TICKER'] == row.TICKER and row.DATE < event['DATE'] <= through:
                shares *= Decimal(str(event['RATIO']))
        result[row.TICKER] += shares
    return result


def anomalies(paths, required, events):
    """Heuristic candidates only; never invent a split from a price jump."""
    prices = pd.read_csv(paths['price_history'], index_col=0) if Path(paths['price_history']).exists() else pd.DataFrame()
    known = {(x['TICKER'], x['DATE']) for x in events}
    result = []
    ratios = [2,3,4,5,10,20,0.5,1/3,0.25,0.2,0.1,0.05]
    for t in required:
        if t not in prices or not required[t]['applicable']:
            continue
        values = pd.to_numeric(prices[t],errors='coerce').dropna()
        values = values[values > 0]
        for i in range(1,len(values)):
            day = str(values.index[i])[:10]
            previous = str(values.index[i-1])[:10]
            if day < required[t]['from'] or (pd.Timestamp(day)-pd.Timestamp(previous)).days > 7:
                continue
            observed = float(values.iloc[i-1]/values.iloc[i])
            closest = min(ratios,key=lambda r:abs(observed/r-1))
            if abs(observed/closest-1)<0.08 and not any(t==symbol and previous<event_day<=day for symbol,event_day in known):
                result.append({'ticker':t,'date':day,'previous_date':previous,'price_ratio':observed,'candidate_split_ratio':closest,
                               'status':'needs_review','note':'Could be a market move or incompatible price basis; not an inferred split.'})
    return result


def run(source, output, through, fetch=False):
    import portfolio_engine as engine
    import portfolio_replay as replay
    import split_history
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError('Audit output must be outside the source directory')
    if output.exists():
        raise ValueError('Choose a new output directory to preserve prior audit evidence')
    output.mkdir(parents=True)
    original = {str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
    shutil.copytree(source,output/'portfolios')
    engine.PORTFOLIOS_DIR=str(output/'portfolios')
    evidence=json.loads((ROOT/'audit/split_evidence.json').read_text())
    verified={(r['ticker'],r['date']):r for r in evidence['events']}
    report=[]
    for pid,_ in engine.list_portfolios():
        paths=engine.get_paths(pid)
        if fetch: split_history.sync(paths,through,force=True)
        tx=pd.read_csv(paths['transactions'],dtype=str).fillna('')
        events=pd.read_csv(paths['splits']).to_dict('records') if Path(paths['splits']).exists() else []
        shares=quantities(tx,events,through)
        snapshot=replay.build_snapshot(paths,valuation_date=through)
        actual={r['ticker']:Decimal(str(r['shares'])) for r in snapshot['actual']['holdings']}
        coverage=split_history.summary(paths,through)
        required=split_history.requirements(paths)
        checks=[]
        for t,n in sorted(shares.items()):
            applicable=[e for e in events if e['TICKER']==t and required[t]['from']<e['DATE']<=through]
            event_checks=[]
            for e in applicable:
                other=verified.get((t,e['DATE']))
                event_checks.append({**e,'independent_status':'confirmed' if other and Decimal(str(other['ratio']))==Decimal(str(e['RATIO'])) else 'needs_review','source_url':other['source_url'] if other else None})
            missing=[e for (ticker,day),e in verified.items() if ticker==t and required[t]['from']<day<=through and not any(x['DATE']==day for x in applicable)]
            checks.append({'ticker':t,'ledger_shares':str(n),'engine_shares':str(actual.get(t)),
                           'shares_match':t in actual and abs(n-actual[t])<=Decimal('0.0000001'),
                           'provider_coverage':next(r for r in coverage['rows'] if r['ticker']==t),
                           'events':event_checks,'missing_independently_confirmed_events':missing})
        item={'portfolio':pid,'as_of':through,'holdings':checks,'unexpected_engine_holdings':sorted(set(actual)-set(shares)),
              'price_jump_candidates':anomalies(paths,required,events),'scope_notes':evidence['scope_notes']}
        report.append(item)
        print(pid,'holdings',len(checks),'share mismatches',sum(not x['shares_match'] for x in checks),'price jump candidates',len(item['price_jump_candidates']),flush=True)
    assert original=={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()},'Input data changed'
    (output/'report.json').write_text(json.dumps(report,indent=2))
    lines=['# Split-history audit',f'As of {through}. Local copies only; no deployments or source-ledger edits.','',
           'Independent event confirmation is distinct from provider coverage. A clean scan cannot prove absence of every unreported event.','',
           '| Portfolio | Holding | Ledger shares | Engine shares | Provider status | Applicable events confirmed |','| --- | --- | ---: | ---: | --- | --- |']
    for p in report:
        for r in p['holdings']:
            lines.append(f"| {p['portfolio']} | {r['ticker']} | {r['ledger_shares']} | {r['engine_shares']} | {r['provider_coverage']['status']} | {sum(e['independent_status']=='confirmed' for e in r['events'])}/{len(r['events'])} |")
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--as-of',required=True);parser.add_argument('--fetch',action='store_true')
    args=parser.parse_args()
    run(args.source.resolve(),args.output.resolve(),args.as_of,args.fetch)
