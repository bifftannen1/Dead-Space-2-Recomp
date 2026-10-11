import os
import sys,json,gzip,csv,re,collections,bisect,glob
csv.field_size_limit(1<<30)
FN=re.compile(r'DEFINE_REX_FUNC\(sub_([0-9A-F]{8})\) \{(.*?)\n\}\n',re.S)
ASM=re.compile(r'^\t// (.+)$',re.M)
def formed(ins):
    """addresses built by lis rX,hi + (addi|ori|load/store off(rX)) -> list in instruction order"""
    hi={}; out=[]
    for i in ins:
        p=i.split(' ',1); op=p[0]; a=[x.strip() for x in p[1].split(',')] if len(p)>1 else []
        if op=='lis' and len(a)==2:
            try: hi[a[0]]=(int(a[1])&0xFFFF)<<16
            except: pass
            continue
        if op in('addi','ori') and len(a)==3 and a[1] in hi:
            try:
                lo=int(a[2]); lo=lo if op=='ori' else lo
                out.append((hi[a[1]]+(lo if op=='ori' else (lo if lo<0x8000 else lo)))&0xFFFFFFFF)
            except: pass
            if a[0]!=a[1]: pass
            continue
        if a:
            mo=re.fullmatch(r'(-?\d+)\((r\d+)\)',a[-1])
            if mo and mo.group(2) in hi: out.append((hi[mo.group(2)]+int(mo.group(1)))&0xFFFFFFFF)
    return out
def load(d):
    F={}
    for f in glob.glob(d+'/ds2_rex_diagnostic_recomp.*.cpp'):
        for m in FN.finditer(open(f,encoding='utf-8',errors='replace').read()):
            F[int(m.group(1),16)]=formed(ASM.findall(m.group(2)))
    return F
A=load(sys.argv[1]); B=load(sys.argv[2])
gt=json.load(open('tu2_ground_truth.json'))
fm={int(f['base'],16):int(f['tu2'],16) for f in gt['functions'] if f.get('status')=='SAME'}
votes=collections.defaultdict(collections.Counter)
for a,b in fm.items():
    fa,fb=A.get(a,[]),B.get(b,[])
    if len(fa)!=len(fb): continue
    for x,y in zip(fa,fb):
        if 0x82000000<=x<0x83000000: votes[x][y-x]+=1
dmap={x:x+c.most_common(1)[0][0] for x,c in votes.items() if len(c)==1}
amb=sum(1 for c in votes.values() if len(c)>1)
# piecewise delta for unvoted data addresses: nearest voted addresses below and above must agree
keys=sorted(dmap)
def data_tr(x):
    if x in dmap: return dmap[x]
    i=bisect.bisect_right(keys,x)
    if 0<i<len(keys) and dmap[keys[i-1]]-keys[i-1]==dmap[keys[i]]-keys[i]: return x+dmap[keys[i]]-keys[i]
    return None
# code: function start map (+ offset within SAME functions)
starts=sorted(fm)
def code_tr(x):
    i=bisect.bisect_right(starts,x)-1
    if i<0: return None
    a=starts[i]; return fm[a]+(x-a) if x-a<0x10000 else None
TEXT=(0x821D0000,0x821D0000+9642748)
rows=list(csv.DictReader(gzip.open(os.environ['DS2_BASE_EVMAP'],'rt',encoding='utf-8'),delimiter='\t',quoting=csv.QUOTE_NONE))
c=collections.Counter()
for r in rows:
    if not r['retail_addr']: continue
    x=int(r['retail_addr'],16)
    t=code_tr(x) if TEXT[0]<=x<TEXT[1] else data_tr(x)
    if r['kind'] in('proc','public_code') and x not in fm: t=None   # only proven SAME starts
    if t is None:
        r['retail_addr']=''; r['bytes_class']='REFUSED'; r['name_class']='REFUSED_TU2'; c['refused']+=1
    else:
        r['retail_addr']='0x%08X'%t; c['kept']+=1
with gzip.open('tu2_evidence_map.tsv.gz','wt',encoding='utf-8',newline='') as o:
    w=csv.DictWriter(o,fieldnames=list(rows[0]),delimiter='\t',quoting=csv.QUOTE_NONE,lineterminator='\n'); w.writeheader(); w.writerows(rows)
print('data addrs voted',len(dmap),'ambiguous',amb,'| evidence rows',dict(c))
