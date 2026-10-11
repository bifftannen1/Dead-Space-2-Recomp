import os
import glob,struct,sys,re,collections
def img(tag):
    secs=[]
    for f in glob.glob(f'img/{tag}.*.bin'):
        _,va,k,_=f.split('/')[-1].split('.')
        secs.append((int(va,16),k,open(f,'rb').read()))
    return secs
def funcs(d):
    S=set()
    for f in glob.glob(d+'/ds2_rex_diagnostic_recomp.*.cpp'):
        S.update(int(x,16) for x in re.findall(r'DEFINE_REX_FUNC\(sub_([0-9A-F]{8})\)',open(f,encoding='utf-8',errors='replace').read()))
    return S
def labels(d):
    L=set()
    for f in glob.glob(d+'/ds2_rex_diagnostic_recomp.*.cpp'):
        L.update(int(x,16) for x in re.findall(r'^loc_([0-9A-F]{8}):',open(f,encoding='utf-8',errors='replace').read(),re.M))
    return L
def scan(tag,known,lab=frozenset()):
    secs=img(tag); tx=[s for s in secs if s[1]=='x'][0]; tlo,tb=tx[0],tx[2]; thi=tlo+len(tb)
    w=lambda a: struct.unpack('>I',tb[a-tlo:a-tlo+4])[0]
    ENDS={0x4E800020,0x4E800420,0x00000000}
    def isend(x): return x in ENDS or (x>>26)==18 and not (x&1)   # blr, bctr, pad, unconditional b
    cands=collections.Counter()
    for va,k,b in secs:
        if k!='d': continue
        for o in range(0,len(b)-3,4):
            p=struct.unpack('>I',b[o:o+4])[0]
            if tlo<p<thi and p%4==0 and p not in known and p not in lab and isend(w(p-4)) and w(p)!=0:
                cands[p]+=1
    # code-built pointers: lis rD,hi ; then addi rX,rD,lo (signed) within 8 insns
    n=len(tb)//4
    for i in range(n):
        x=struct.unpack('>I',tb[i*4:i*4+4])[0]
        if x>>26!=15 or (x>>16)&31!=0: continue
        rd=(x>>21)&31; hi=(x&0xFFFF)<<16
        for j in range(i+1,min(n,i+9)):
            y=struct.unpack('>I',tb[j*4:j*4+4])[0]
            if y>>26==14 and (y>>16)&31==rd:
                lo=y&0xFFFF; lo=lo-0x10000 if lo&0x8000 else lo
                p=(hi+lo)&0xFFFFFFFF
                if tlo<p<thi and p%4==0 and p not in known and p not in lab and isend(w(p-4)) and w(p)!=0: cands[p]+=1
                break
    return cands
known_base=funcs(os.environ['DS2_BASE_GEN'])
# precision test on base: pretend we know only base functions minus a random 10% held out
import random; random.seed(1)
hold=set(random.sample(sorted(known_base),4000))
c=scan('base',known_base-hold,labels(os.environ['DS2_BASE_GEN']))
tp=sum(1 for p in c if p in hold); fp=sum(1 for p in c if p not in hold)
print('base check: candidates',len(c),'recovered held-out',tp,'of',len(hold),'| not real functions',fp)
known_tu2=funcs(os.environ['DS2_TU2_GEN'])
c2=scan('tu2',known_tu2,labels(os.environ['DS2_TU2_GEN']))
open('ptr_seeds_tu2.txt','w').write('\n'.join('0x%08X'%p for p in sorted(c2)))
print('tu2: new pointer-target candidates',len(c2), 'contains crash 0x828CD928:',0x828CD928 in c2)
