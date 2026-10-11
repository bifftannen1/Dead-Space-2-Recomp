"""Step 2: place TU2-only functions by proven neighbours. Rule: the nearest proven neighbour below and
above (TU2 address order) are in the same main file -> the function is in that file. Measured first by
leave-one-out on proven rows; applied only if LOO >= 0.99 (Wilson lower bound)."""
import gzip,csv,bisect,math
csv.field_size_limit(1<<30)
rows=list(csv.DictReader(gzip.open('tu2/config/allocation_tu2.tsv.gz','rt',encoding='utf-8'),delimiter='\t',quoting=csv.QUOTE_NONE))
cols=list(rows[0])
proven=sorted((int(r['addr'],16),r['tree_path']) for r in rows if 'tu2=P' in r['basis'] and r['class']!='UNDETERMINABLE' and r['tree_path'])
ks=[a for a,_ in proven]
def pred(a,skip=None):
    i=bisect.bisect_left(ks,a); lo=i-1; hi=i
    if skip is not None:
        if hi<len(ks) and ks[hi]==a: hi+=1
    K=2
    if lo-K+1<0 or hi+K-1>=len(ks): return None
    fs={proven[j][1] for j in list(range(lo-K+1,lo+1))+list(range(hi,hi+K))}
    return fs.pop() if len(fs)==1 else None
ok=dec=0
for a,t in proven:
    p=pred(a,skip=True)
    if p is not None: dec+=1; ok+=(p==t)
z=1.96; ph=ok/dec; wl=(ph+z*z/(2*dec)-z*math.sqrt(ph*(1-ph)/dec+z*z/(4*dec*dec)))/(1+z*z/dec)
print(f'LOO on proven rows: {ok}/{dec} = {ph:.4%}, Wilson95 lower {wl:.4f}')
pc={r['addr']:r for r in csv.DictReader(gzip.open('tu2/build/pdbchain_tu2/allocation.tsv.gz','rt',encoding='utf-8'),delimiter='\t',quoting=csv.QUOTE_NONE)}
nm={r['addr']:r for r in csv.DictReader(gzip.open('tu2/build/pdbchain_tu2/names.tsv.gz','rt',encoding='utf-8'),delimiter='\t',quoting=csv.QUOTE_NONE)}
bypath={}
for r in rows:
    if r['tree_path']: bypath.setdefault(r['tree_path'],r)
nb=pcn=und=agree=0
for r in rows:
    if r['basis']!='tu2_unmapped': continue
    a=int(r['addr'],16); p=pred(a) if wl>=0.99 else None; q=pc.get(r['addr'])
    if p:
        t=bypath[p]
        for k in ('class','obj','lib','src_file','tree_path'): r[k]=t[k]
        r['class']='NEIGHBOUR'; r['basis']='tu2_unmapped;tu2=neighbour'; nb+=1
        if q and q['tree_path']==p: agree+=1
    elif q and q['class']!='UNDETERMINABLE' and q['tree_path']:
        for k in cols:
            if k in q and k not in('addr','symbol'): r[k]=q[k]
        r['basis']='tu2_unmapped;tu2=pdbchain'; pcn+=1
    else: und+=1
    if q and r['addr'] in nm and r.get('name_status','NONE')=='NONE':
        r['pdb_name']=nm[r['addr']]['pdb_name']; r['ident']=nm[r['addr']]['ident']; r['name_status']='NAME_'+nm[r['addr']]['method']
with gzip.open('tu2/config/allocation.tsv.gz','wt',encoding='utf-8',newline='') as f:
    w=csv.DictWriter(f,fieldnames=cols,delimiter='\t',quoting=csv.QUOTE_NONE,lineterminator='\n'); w.writeheader(); w.writerows(rows)
print(f'TU2-only rows: neighbour-placed {nb} (pdbchain agreed on {agree}), pdbchain-placed {pcn}, undeterminable {und}')
