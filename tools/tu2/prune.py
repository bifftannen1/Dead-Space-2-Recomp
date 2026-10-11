import re,bisect,tomllib
log=open('build/logs/codegen.log',encoding='utf-8',errors='replace').read()
pairs={(int(t,16),int(f,16)) for t,f in re.findall(r'Unresolved (?:\w+ )?(?:branch to|b target) 0x([0-9A-F]+) from 0x([0-9A-F]+)',log)}
p='app/ds2_rex_diagnostic_manifest.toml'; s=open(p,newline='').read()
base_keep=set(open('../seeds_tu2.txt').read().split())
seeds=sorted(int(a,16) for a in re.findall(r'^(0x[0-9A-F]+) = ',s,re.M))
bad=set()
for t,f in pairs:
    lo,hi=min(t,f),max(t,f)
    i=bisect.bisect_right(seeds,lo)
    j=bisect.bisect_right(seeds,lo)-1
    if j>=0 and '0x%08X'%seeds[j] not in base_keep: bad.add(seeds[j])
    while i<len(seeds) and seeds[i]<=hi:
        if '0x%08X'%seeds[i] not in base_keep: bad.add(seeds[i])
        i+=1
imp=0x82B0771C
# R8: never keep a seed on a codegen helper entry (__save*/__rest* in the previous build's
# registration table, moved through the proven map: majority delta of the 10 nearest proven pairs)
import json,collections,os
helpers=set()
_gt=os.environ.get('DS2_GT','../tu2_ground_truth.json'); _preg=os.environ.get('DS2_PREV_REGISTER')
if os.path.exists(_gt) and _preg and os.path.exists(_preg):
    _fm=sorted((int(f['base'],16),int(f['tu2'],16)) for f in json.load(open(_gt))['functions'] if f.get('status')=='SAME')
    _ka=[a for a,_ in _fm]
    for _a in re.findall(r'SetFunction\((0x[0-9A-F]{8}), __(?:save|rest)\w+\)',open(_preg).read()):
        _x=int(_a,16); _i=bisect.bisect_right(_ka,_x)
        _d=collections.Counter(b-a for a,b in _fm[max(0,_i-5):_i+5]).most_common(1)[0][0]; helpers.add(_x+_d)
bad|={x for x in seeds if x>=imp}
bad|={x for x in seeds if x in helpers}
print('helper entries protected',len(helpers),'seeds on helpers removed',len({x for x in seeds if x in helpers}))
s=''.join(l for l in s.splitlines(True) if not (re.match(r'^0x[0-9A-F]+ = ',l) and int(l.split()[0],16) in bad))
tomllib.loads(s); open(p,'w',newline='').write(s)
print('unresolved pairs',len(pairs),'removed seeds',len(bad),'left',len(re.findall(r'^0x',s,re.M)))
