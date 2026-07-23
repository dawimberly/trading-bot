import json, csv
from pathlib import Path
p = Path('data/dry_orders.jsonl')
out = Path('data/dry_orders.csv')
rows = []
with p.open(encoding='utf-8') as f:
    for line in f:
        if not line.strip():
            continue
        r=json.loads(line)
        req=r.get('request',{})
        rows.append({
            'id': r.get('id'),
            'symbol': r.get('symbol'),
            'side': req.get('side'),
            'qty': req.get('qty',''),
            'notional': req.get('notional',''),
            'type': req.get('type',''),
            'limit_price': req.get('limit_price',''),
            'at': r.get('at')
        })
with out.open('w', newline='', encoding='utf-8') as f:
    writer=csv.DictWriter(f, fieldnames=['id','symbol','side','qty','notional','type','limit_price','at'])
    writer.writeheader()
    writer.writerows(rows)
print('Wrote', len(rows), 'rows to', out)
