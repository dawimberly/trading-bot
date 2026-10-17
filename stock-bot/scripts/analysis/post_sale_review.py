"""Offline report. Requires exported broker FILLs, explicit calendar and price metadata."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from modules.post_sale_review import post_sale_report

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for key in ('book','fills','calendar','prices','as-of','output'):ap.add_argument('--'+key,required=True)
    ap.add_argument('--plans',nargs='*',default=[])
    ap.add_argument('--initial-inventory',help='Verified inventory at start of fills; omit for unknown. {} explicitly asserts flat.')
    a=ap.parse_args()
    read=lambda p:json.loads(Path(p).read_text(encoding='utf-8-sig'))
    plans=[]
    for p in a.plans:
        plans.extend(json.loads(l) for l in Path(p).read_text(encoding='utf8').splitlines() if l.strip())
    result=post_sale_report(book=a.book,fills=read(a.fills),calendar=read(a.calendar),prices=read(a.prices),as_of=a.as_of,plans=plans,initial_inventory=read(a.initial_inventory) if a.initial_inventory else None)
    target=Path(a.output)
    if target.exists() and target.is_symlink():raise ValueError('refuse symlink output')
    forbidden=[ROOT/'data'/'portal',ROOT/'data'/'decision_tape']
    if any(target.resolve().is_relative_to(p.resolve()) for p in forbidden):raise ValueError('report output must not overwrite book/tape data')
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('x',encoding='utf8') as f:json.dump(result,f,indent=2,allow_nan=False)
    print(f"Wrote observational review: {len(result['purchases'])} purchases, {len(result['sales'])} sales")
if __name__=='__main__':main()
