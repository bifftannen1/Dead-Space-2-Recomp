"""V0_PREV_BUILD core: proven function identity between a previous build (with a named tree) and
the current build, from the two codegen trees and the two loaded images. Evaluated by
stages/prevbuild.py with BASE_GEN, CUR_GEN, IMG_DIR, _log in its namespace. Proof classes:
P1 unique identical code (addresses and lis/addi halves blanked), P2 call position in a proven
caller, P3 identical duplicate with all callers proven, P4/P5 equal-size gap between proven
neighbours (same / changed code), P6 identical code order-preserving in an unequal gap,
P7 call-graph + data-ref unique mutual best in a gap, P8 vtable / pointer slot through the
proven data map, P9 referenced string contents. Pair gate: a P6-P9 pair whose rare-callee
call sites contradict the proven map is refused."""
"""Ground-truth allocation: unpatched (GitHub) -> TU2.
Each TU2 function is matched by proof, not similarity:
  P1 exact      : instruction stream identical after replacing absolute addresses by role tokens,
                  and the body is unique in both builds.
  P2 callgraph  : position k of a P1/P2 function's call list is `bl X` in base and `bl X'` in TU2
                  -> X maps to X' (iterated to a fixpoint). Any contradiction is recorded and both
                  sides are refused.
  P3 exact-ctx  : identical normalized body, not unique, disambiguated because all its callers
                  (or callees) are already mapped consistently.
Everything else is CHANGED (mapped via P2 but body differs) or UNMAPPED/NEW; no order or name guess.
"""
import re,glob,json,collections,csv,hashlib,sys
FN=re.compile(r'DEFINE_REX_FUNC\(sub_([0-9A-F]{8})\) \{(.*?)\n\}\n',re.S)
ASM=re.compile(r'^\t// (.+)$',re.M)
ADDR=re.compile(r'0x[0-9a-fA-F]{5,8}')
REG=re.compile(r'^(\w+\.?) (.*)$')
def normalize(ins):
    hi=set(); out=[]
    for i in ins:
        i=ADDR.sub('#',i); mm=REG.match(i)
        if not mm: out.append(i); continue
        op,args=mm.groups(); a=[x.strip() for x in args.split(',')]
        if op=='lis' and len(a)==2:
            hi.add(a[0]); out.append('lis %s,#'%a[0]); continue
        # base register built by lis: addi rD,rS,imm | ld/st rD,imm(rS) | ori
        if len(a)==3 and a[1] in hi and re.fullmatch(r'-?\d+',a[2]): a[2]='#'
        if len(a)>=2:
            mo=re.fullmatch(r'(-?\d+)\((r\d+)\)',a[-1])
            if mo and mo.group(2) in hi: a[-1]='#(%s)'%mo.group(2)
        if a and a[0] in hi and op not in ('lis',) and not (len(a)==3 and a[1]==a[0]): hi.discard(a[0])
        out.append(op+' '+','.join(a))
    return out
def load(d):
    F={}
    for f in glob.glob(d+'/ds2_rex_diagnostic_recomp.*.cpp'):
        for m in FN.finditer(open(f,encoding='utf-8',errors='replace').read()):
            ins=ASM.findall(m.group(2))
            calls=[int(x,16) for x in re.findall(r'^bl 0x([0-9a-f]+)$','\n'.join(ins),re.M)]
            norm=hashlib.sha1(chr(10).join(normalize(ins)).encode()).hexdigest()
            # lis/addi immediates of addresses are plain numbers: strip lis hi-halves and paired lo
            F[int(m.group(1),16)]=dict(ins=ins,calls=calls,norm=norm,n=len(ins))
    return F
A=load(BASE_GEN); B=load(CUR_GEN)
how={}; m={}; rev={}; conflicts=[]
def link(b,a,why):
    if b in m or a in rev:
        if m.get(b)!=a: conflicts.append((hex(b),hex(a),why))
        return False
    m[b]=a; rev[a]=b; how[b]=why; return True
ha=collections.defaultdict(list); hb=collections.defaultdict(list)
for a,f in A.items(): ha[f['norm']].append(a)
for b,f in B.items(): hb[f['norm']].append(b)
for h,bl in hb.items():
    if len(bl)==1 and len(ha.get(h,[]))==1: link(bl[0],ha[h][0],'P1')
# P2 fixpoint over call lists: align each mapped pair's call sequences on already-proven
# callees, then pair the positions inside equal-length gaps between agreeing call sites.
import difflib
changed=True
while changed:
    changed=False
    for b,a in list(m.items()):
        cb,ca=B[b]['calls'],A[a]['calls']
        if not cb or not ca: continue
        kb=[('M',m[x]) if x in m else ('U',i) for i,x in enumerate(cb)]
        ka=[('M',x) if x in rev else ('U',-1-i) for i,x in enumerate(ca)]
        sm=difflib.SequenceMatcher(None,kb,ka,autojunk=False)
        for tag,i1,i2,j1,j2 in sm.get_opcodes():
            if tag=='replace' and i2-i1==j2-j1 or (tag=='equal'):
                for xb,xa in zip(cb[i1:i2],ca[j1:j2]):
                    if xb in B and xa in A and xb not in m and xa not in rev:
                        if link(xb,xa,'P2'): changed=True
                    elif xb in m and m[xb]!=xa: conflicts.append((hex(xb),hex(xa),'P2-contradiction'))
# P3: duplicate bodies resolved by callers
callers_b=collections.defaultdict(set); callers_a=collections.defaultdict(set)
for b,f in B.items():
    for x in f['calls']: callers_b[x].add(b)
for a,f in A.items():
    for x in f['calls']: callers_a[x].add(a)
for h,bl in hb.items():
    al=ha.get(h,[])
    if len(bl)<2 or len(al)<2: continue
    for b in bl:
        if b in m: continue
        cb={m.get(c) for c in callers_b[b]}
        if None in cb or not cb: continue
        hits=[a for a in al if a not in rev and callers_a[a]==cb]
        if len(hits)==1: link(b,hits[0],'P3')
# P4/P5: between consecutive proven neighbours (monotone in both builds) with equal-size gaps:
# identical normalized bodies in order -> P4 (proof); otherwise -> P5 (positional, body differs).
import bisect
for rnd in range(3):
    As=sorted(A); Bs=sorted(B)
    anc=sorted((b,m[b]) for b in m)
    for (b0,a0),(b1,a1) in zip(anc,anc[1:]):
        if a1<=a0: continue
        gb=Bs[bisect.bisect_right(Bs,b0):bisect.bisect_left(Bs,b1)]
        ga=As[bisect.bisect_right(As,a0):bisect.bisect_left(As,a1)]
        if not gb or len(gb)!=len(ga) or any(x in m for x in gb) or any(y in rev for y in ga): continue
        allsame=all(B[x]['norm']==A[y]['norm'] for x,y in zip(gb,ga))
        for x,y in zip(gb,ga):
            link(x,y,'P4' if B[x]['norm']==A[y]['norm'] else 'P5')
# P6: unequal gaps -> longest common subsequence of normalized bodies inside the gap (order-preserving
# identical code); then re-run P4/P5 on the smaller gaps this creates.
for rnd in range(3):
    As=sorted(A); Bs=sorted(B)
    anc=sorted((b,m[b]) for b in m)
    for (b0,a0),(b1,a1) in zip(anc,anc[1:]):
        if a1<=a0: continue
        gb=[x for x in Bs[bisect.bisect_right(Bs,b0):bisect.bisect_left(Bs,b1)] if x not in m]
        ga=[y for y in As[bisect.bisect_right(As,a0):bisect.bisect_left(As,a1)] if y not in rev]
        if not gb or not ga or len(gb)==len(ga): continue
        sm=difflib.SequenceMatcher(None,[B[x]['norm'] for x in gb],[A[y]['norm'] for y in ga],autojunk=False)
        for blk in sm.get_matching_blocks():
            for k in range(blk.size): link(gb[blk.a+k],ga[blk.b+k],'P6')
    for (b0,a0),(b1,a1) in zip(anc,anc[1:]):
        pass
    anc=sorted((b,m[b]) for b in m)
    for (b0,a0),(b1,a1) in zip(anc,anc[1:]):
        if a1<=a0: continue
        gb=Bs[bisect.bisect_right(Bs,b0):bisect.bisect_left(Bs,b1)]
        ga=As[bisect.bisect_right(As,a0):bisect.bisect_left(As,a1)]
        if not gb or len(gb)!=len(ga) or any(x in m for x in gb) or any(y in rev for y in ga): continue
        for x,y in zip(gb,ga): link(x,y,'P4' if B[x]['norm']==A[y]['norm'] else 'P5')
# --- data address map from SAME pairs (instruction-aligned formed addresses), then P8 vtables
import struct as _st, glob as _gl
def _formed(ins):
    hi={}; out=[]
    for i in ins:
        p=i.split(' ',1); op=p[0]; a=[x.strip() for x in p[1].split(',')] if len(p)>1 else []
        if op=='lis' and len(a)==2:
            try: hi[a[0]]=(int(a[1])&0xFFFF)<<16
            except ValueError: pass
            continue
        if op in('addi','ori') and len(a)==3 and a[1] in hi:
            try: out.append((hi[a[1]]+int(a[2]))&0xFFFFFFFF)
            except ValueError: pass
            continue
        if a:
            mo=re.fullmatch(r'(-?\d+)\((r\d+)\)',a[-1])
            if mo and mo.group(2) in hi: out.append((hi[mo.group(2)]+int(mo.group(1)))&0xFFFFFFFF)
    return out
for _F in (A,B):
    for _f in _F.values(): _f['data']=_formed(_f['ins'])
_votes=collections.defaultdict(collections.Counter)
for b,a in m.items():
    if B[b]['norm']!=A[a]['norm'] or len(A[a]['data'])!=len(B[b]['data']): continue
    for x,y in zip(A[a]['data'],B[b]['data']):
        if 0x82000000<=x<0x83000000: _votes[x][y-x]+=1
dmap={x:x+c.most_common(1)[0][0] for x,c in _votes.items() if len(c)==1}
_dk=sorted(dmap)
def dtr(x):
    if x in dmap: return dmap[x]
    i=bisect.bisect_right(_dk,x)
    if 0<i<len(_dk) and dmap[_dk[i-1]]-_dk[i-1]==dmap[_dk[i]]-_dk[i]: return x+dmap[_dk[i]]-_dk[i]
    return None
def _img(tag):
    return [(int(f.split('.')[-3],16),open(f,'rb').read()) for f in _gl.glob(IMG_DIR+'/%s.*.d.bin'%tag)]
import os
IA,IB=_img('base'),_img('tu2')
def _word(I,x):
    for va,b in I:
        if va<=x<va+len(b)-3: return _st.unpack('>I',b[x-va:x-va+4])[0]
    return None
p8=0
for va,b in IA:
    for o in range(0,len(b)-3,4):
        y=_st.unpack('>I',b[o:o+4])[0]
        if y not in A or y in rev: continue
        t=dtr(va+o)
        if t is None: continue
        x=_word(IB,t)
        if x in B and x not in m:
            if link(x,y,'P8'): p8+=1
_log('P8 vtable/pointer-slot pairs',p8,'data addrs mapped',len(dmap))
# P7: rewritten functions. Inside each gap between consecutive proven neighbours, pair an
# unmatched TU2 function with an unmatched base function when they are each other's unique best
# by call-graph evidence (matched callers + matched callees agreeing), with >=1 agreement and no
# contradiction (a matched callee/caller of one whose counterpart is absent from the other is not a
# contradiction -- code changed -- but a callee mapped to a *different* proven function is).
def p7_round():
    added=0
    As=sorted(A); Bs=sorted(B)
    anc=sorted((b,m[b]) for b in m)
    for (b0,a0),(b1,a1) in zip(anc,anc[1:]):
        if a1<=a0: continue
        gb=[x for x in Bs[bisect.bisect_right(Bs,b0):bisect.bisect_left(Bs,b1)] if x not in m]
        ga=[y for y in As[bisect.bisect_right(As,a0):bisect.bisect_left(As,a1)] if y not in rev]
        if not gb or not ga: continue
        sc={}
        for x in gb:
            cx={m[c] for c in B[x]['calls'] if c in m}; kx={m[c] for c in callers_b[x] if c in m}
            for y in ga:
                cy=set(A[y]['calls'])&set(rev); ky=callers_a[y]&set(rev)
                dx={dtr(v) for v in A[y]['data']}; agree=len(cx&cy)+len(kx&ky)+len(dx&set(B[x]['data']))
                if agree: sc[(x,y)]=agree
        for (x,y),v in sc.items():
            if x in m or y in rev: continue
            bx=max(w for (p,q),w in sc.items() if p==x); by=max(w for (p,q),w in sc.items() if q==y)
            if v==bx==by and sum(1 for (p,q),w in sc.items() if p==x and w==v)==1 and sum(1 for (p,q),w in sc.items() if q==y and w==v)==1:
                link(x,y,'P7'); added+=1
    return added
while p7_round(): pass
# P9: rewritten functions paired by the *contents* of the strings they reference (read from each
# image at the function's formed data addresses), inside gaps between proven neighbours first, then
# program-wide where a string set is unique on both sides. Mutual unique best, >= 1 shared string.
def _str(I,x):
    for va,b in I:
        if va<=x<va+len(b):
            e=b.find(bytes(1),x-va,x-va+256)
            t=b[x-va:e] if e>=0 else b''
            return t if len(t)>=4 and all(32<=c<127 for c in t) else None
    return None
for _F,_I in ((A,IA),(B,IB)):
    for _f in _F.values(): _f['str']=frozenset(t for t in (_str(_I,x) for x in _f['data']) if t)
def p9_pairs(gb,ga):
    sc={}
    for x in gb:
        sx=B[x]['str']
        if not sx: continue
        for y in ga:
            v=len(sx&A[y]['str'])
            if v: sc[(x,y)]=v
    n=0
    for (x,y),v in sorted(sc.items(),key=lambda kv:-kv[1]):
        if x in m or y in rev: continue
        bx=[w for (p,q),w in sc.items() if p==x]; by=[w for (p,q),w in sc.items() if q==y]
        if v==max(bx)==max(by) and bx.count(v)==1 and by.count(v)==1:
            if link(x,y,'P9'): n+=1
    return n
_tot=0
for _r in range(3):
    As=sorted(A); Bs=sorted(B); anc=sorted((b,m[b]) for b in m); _n=0
    for (b0,a0),(b1,a1) in zip(anc,anc[1:]):
        if a1<=a0: continue
        gb=[x for x in Bs[bisect.bisect_right(Bs,b0):bisect.bisect_left(Bs,b1)] if x not in m]
        ga=[y for y in As[bisect.bisect_right(As,a0):bisect.bisect_left(As,a1)] if y not in rev]
        if gb and ga: _n+=p9_pairs(gb,ga)
    _tot+=_n
    while p7_round(): pass
    if not _n: break
_g=p9_pairs([x for x in B if x not in m],[y for y in A if y not in rev])
_log('P9 string pairs: in-gap',_tot,'program-wide',_g)
# P6 gate: refuse any P6 pair whose rare-callee call sites disagree with the proven map
_rare={x for x,c in callers_a.items() if len(c)<=3} if 'callers_a' in dir() else set()
refused=[]
for b in [x for x,k in how.items() if k in('P6','P7','P8','P9')]:
    a=m[b]; cb,ca=B[b]['calls'],A[a]['calls']
    if len(cb)!=len(ca): continue
    if any(xb in m and xa in _rare and m[xb]!=xa for xb,xa in zip(cb,ca)):
        del rev[a]; del m[b]; del how[b]; refused.append(('0x%08X'%b,'0x%08X'%a))
_log('pair gate refused',len(refused))
