import os
import re,glob,hashlib,collections,csv,bisect
H=re.compile(r'0x[0-9a-fA-F]{4,8}|sub_[0-9A-F]{8}|loc_[0-9A-F]{8}|-?\d{4,}')
def load(d):
    fn={}
    for f in glob.glob(d+'/ds2_rex_diagnostic_recomp.*.cpp'):
        s=open(f,encoding='utf-8',errors='replace').read()
        for m in re.finditer(r'DEFINE_REX_FUNC\(sub_([0-9A-F]{8})\) \{(.*?)\n\}\n',s,re.S):
            b=m.group(2); ins=len(re.findall(r'\n\t// ',b))
            fn[int(m.group(1),16)]=(hashlib.sha1(H.sub('#',b).encode()).hexdigest(),ins)
    return fn
A=load(os.environ['DS2_BASE_GEN']); B=load(os.environ['DS2_TU2_GEN'])
names={}
for r in csv.reader(open('DS2Recomp-Source/src/symbols.tsv'),delimiter='\t'):
    if r[0].startswith('0x') and not r[2].startswith('sub_'): names[int(r[0],16)]=r[2]
named_base=sum(1 for a in A if a in names)
ha=collections.defaultdict(list); hb=collections.defaultdict(list)
for a,(h,_) in A.items(): ha[h].append(a)
for b,(h,_) in B.items(): hb[h].append(b)
m={}  # tu2 -> base
for h,bl in hb.items():
    al=ha.get(h,[])
    if len(al)==1 and len(bl)==1: m[bl[0]]=al[0]
# order fill: walk anchors in tu2 order; between consecutive anchors with monotone base, pair equal-count gaps
As=sorted(A); Bs=sorted(B)
anc=sorted((b,a) for b,a in m.items())
fill=0
for (b0,a0),(b1,a1) in zip(anc,anc[1:]):
    if a1<=a0: continue
    gb=Bs[bisect.bisect_right(Bs,b0):bisect.bisect_left(Bs,b1)]
    ga=As[bisect.bisect_right(As,a0):bisect.bisect_left(As,a1)]
    if gb and len(gb)==len(ga):
        for x,y in zip(gb,ga):
            if x not in m: m[x]=y; fill+=1
# monotonic sanity: fraction of anchors in order
inv=sum(1 for (b0,a0),(b1,a1) in zip(anc,anc[1:]) if a1<a0)
got=sum(1 for b,a in m.items() if a in names)
print(f'base named {named_base}/{len(A)}; tu2 funcs {len(B)}')
print(f'unique body anchors {len(anc)} (order breaks {inv}); gap fill +{fill}; mapped {len(m)}; names carried {got} ({got/named_base:.1%} of base names)')

# translate base manifest seeds to TU2 via nearest anchor at or below
import re as _re
base_seeds=[int(a,16) for a in _re.findall(r'^(0x[0-9A-F]+) = \{',open('tu2/app/manifest.base.toml').read(),_re.M)]
anc_ab=sorted((a,b) for b,a in m.items())
ka=[a for a,_ in anc_ab]
out=[]
for s in base_seeds:
    i=bisect.bisect_right(ka,s)
    near=anc_ab[max(0,i-5):i+5]
    d=collections.Counter(b-a for a,b in near).most_common(1)[0][0]
    out.append(s+d)
open('seeds_tu2.txt','w').write('\n'.join('0x%08X'%x for x in out))
print('dups',len(out)-len(set(out)),'translated seeds',len(out),'examples',[('%08X'%s,'%08X'%t) for s,t in list(zip(base_seeds,out))[-4:]])
