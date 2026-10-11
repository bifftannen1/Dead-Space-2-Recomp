import json,gzip,csv,sys,re
sys.path.insert(0,'tools')
import organize_generated as O
from pathlib import Path
by=O.load_allocation(Path('config/allocation.tsv.gz')); O.apply_names(by,Path('config/names.tsv.gz'))
gt=json.load(open('tu2_ground_truth.json'))
cols=list(next(iter(by.values())).keys())
rows=[]; c={'carried':0,'undet':0}
for f in gt['functions']:
    t=int(f['tu2'],16)
    if 'base' in f and int(f['base'],16) in by:
        a=int(f['base'],16); r=dict(by[a])
        oh='%08X'%a; nh='%08X'%t
        r['addr']='0x'+nh; r['symbol']='sub_'+nh
        if r.get('ident'): r['ident']=r['ident'].replace(oh,nh)
        r['basis']=r['basis']+';tu2=%s:%s'%(f['proof'],f['base'])
        c['carried']+=1
    else:
        r={k:'' for k in cols}; r.update(addr='0x%08X'%t,symbol='sub_%08X'%t,**{'class':'UNDETERMINABLE'},basis='tu2_unmapped',name_status='NONE')
        c['undet']+=1
    rows.append(r)
recomp,blocks,declared,reg,units=O.read_generated(Path('app/generated'))
bysym={r['symbol']:r for r in by.values()}
have={r['addr'] for r in rows}
for sym in blocks:
    a=reg.get(sym)
    if a is None or '0x%08X'%a in have: continue
    r=dict(bysym[sym]) if sym in bysym else {k:'' for k in cols}
    if sym not in bysym: r.update(symbol=sym,**{'class':'UNDETERMINABLE'},basis='tu2_helper',name_status='NONE')
    r['addr']='0x%08X'%a; rows.append(r); c['helpers']=c.get('helpers',0)+1
with gzip.open('build/tu2/allocation_script.tsv.gz','wt',encoding='utf-8',newline='') as o:
    w=csv.DictWriter(o,fieldnames=cols,delimiter='\t',quoting=csv.QUOTE_NONE,escapechar=None,extrasaction='ignore',lineterminator='\n'); w.writeheader(); w.writerows(rows)
with gzip.open('build/tu2/names_script.tsv.gz','wt',encoding='utf-8',newline='') as o:
    o.write('addr\tmethod\tpdb_name\tident\tproto_va\tobj_check\tevidence\taliases\n')
print(c)
