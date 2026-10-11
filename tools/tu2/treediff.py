import os,re,collections,sys,json
BS=chr(92)
def strip(x): return re.sub(r'_[0-9A-F]{8}$','',x)
def tree(root):
    files={};names=collections.Counter();unnamed=0;inline=0
    for d,_,fs in os.walk(root):
        for f in fs:
            if not f.endswith('.cpp'): continue
            p=os.path.relpath(os.path.join(d,f),root).replace(BS,'/')
            p=re.sub(r'\.part\d+\.cpp$','.cpp',p)
            s=open(os.path.join(d,f),encoding='utf-8',errors='replace').read()
            defs=re.findall(r'DEFINE_REX_FUNC\(([A-Za-z0-9_]+)\)',s)
            if p.startswith('_units/'): inline+=len(defs); continue
            files.setdefault(p,[]).extend(defs)
            for x in defs:
                if re.fullmatch(r'sub_[0-9A-F]{8}',x): unnamed+=1
                else: names[strip(x)]+=1
    return files,names,unnamed,inline
B=tree(sys.argv[1]); T=tree(sys.argv[2])
fb,ft=set(B[0]),set(T[0])
gen=lambda S:sum(1 for p in S if p.startswith('other/') or '/other/' in p)
out={'files_main':len(fb),'files_tu2':len(ft),'only_main':sorted(fb-ft),'only_tu2':sorted(ft-fb),
 'generic_main':gen(fb),'generic_tu2':gen(ft),'inline_main':B[3],'inline_tu2':T[3],'unnamed_main':B[2],'unnamed_tu2':T[2]}
dif=[]
for p in sorted(fb&ft):
    cb=collections.Counter(strip(x) for x in B[0][p]); ct=collections.Counter(strip(x) for x in T[0][p])
    if cb!=ct: dif.append({'file':p,'only_main':sorted((cb-ct).elements()),'only_tu2':sorted((ct-cb).elements())})
out['shared_files_differing']=dif
json.dump(out,open('tu2_tree_diff.json','w'),indent=1)
print({k:(len(v) if isinstance(v,list) else v) for k,v in out.items()})
print('name overlap',sum((B[1]&T[1]).values()),'main named',sum(B[1].values()),'tu2 named',sum(T[1].values()))
