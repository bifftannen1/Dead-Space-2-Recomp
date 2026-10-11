import gzip,csv
csv.field_size_limit(1<<30)
def rows(p): return list(csv.DictReader(gzip.open(p,'rt',encoding='utf-8'),delimiter='\t',quoting=csv.QUOTE_NONE))
ours=rows('build/tu2/allocation_script.tsv.gz'); cols=list(ours[0])
pc={r['addr']:r for r in rows('build/pdbchain_tu2/allocation.tsv.gz')}
nm={r['addr']:r for r in csv.DictReader(gzip.open('build/pdbchain_tu2/names.tsv.gz','rt',encoding='utf-8'),delimiter='\t',quoting=csv.QUOTE_NONE)}
n=0
for o in ours:
    a=o['addr']; p=pc.get(a)
    if o['basis']=='tu2_unmapped' and p and p['class']!='UNDETERMINABLE' and p['tree_path']:
        for k in cols:
            if k in p and k not in('addr','symbol'): o[k]=p[k]
        o['basis']=(o['basis']+';tu2=pdbchain')
        if a in nm and o['name_status']=='NONE':
            o['pdb_name']=nm[a]['pdb_name']; o['ident']=nm[a]['ident']; o['name_status']='NAME_'+nm[a]['method']
        n+=1
with gzip.open('config/allocation.tsv.gz','wt',encoding='utf-8',newline='') as f:
    w=csv.DictWriter(f,fieldnames=cols,delimiter='\t',quoting=csv.QUOTE_NONE,lineterminator='\n'); w.writeheader(); w.writerows(ours)
print('pdbchain merged (tu2-only rows)',n)
