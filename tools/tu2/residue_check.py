import json,re,csv,gzip,collections
csv.field_size_limit(1<<30)
strip=lambda x:re.sub(r'_[0-9A-F]{8}$','',x)
gt=json.load(open('tu2_ground_truth.json')); d=json.load(open('tu2_tree_diff.json'))
rows={r['addr']:r for r in csv.DictReader(gzip.open('tu2/config/allocation.tsv.gz','rt',encoding='utf-8'),delimiter='\t',quoting=csv.QUOTE_NONE)}
res=[dict(side='main_only',addr=f['base'],name=f['name'],file=f['file'],reason='rewritten or removed in TU2: no P1-P9 proof') for f in gt['base_unmatched']]
for f in gt['functions']:
    if f['status']=='UNMAPPED':
        r=rows.get(f['tu2'],{})
        res.append(dict(side='tu2_only',addr=f['tu2'],name=r.get('pdb_name',''),file=r.get('tree_path',''),placed_by='pdbchain' if 'pdbchain' in r.get('basis','') else 'undeterminable',reason='TU2 code with no proven main twin'))
json.dump(res,open('tu2/docs/tu2_residue.json','w'),indent=1)
ident={}
for r in csv.reader(open('DS2Recomp-Source/src/symbols.tsv'),delimiter='\t'):
    if r[0].startswith('0x'): ident.setdefault(strip(r[2]),set()).add('0x'+r[0][2:].upper())
mo={x['addr'] for x in res if x['side']=='main_only'}; to={x['addr'] for x in res if x['side']=='tu2_only'}
tid={}
for r in rows.values(): tid.setdefault(strip(r['ident']) if r['ident'] else r['symbol'],set()).add(r['addr'])
ok=bad=0; badl=[]
for f in d['shared_files_differing']:
    for side,lst,idx,S in (('main',f['only_main'],ident,mo),('tu2',f['only_tu2'],tid,to)):
        for n in lst:
            if n=='sub': continue
            if idx.get(n,set())&S: ok+=1
            else: bad+=1; badl.append((side,f['file'],n))
mfile=collections.defaultdict(set)
for x in res: mfile[x['file']].add(x['side'])
fo=[p for p in d['only_main'] if 'main_only' not in mfile.get(p,set()) and not p.startswith('app/')]
ft=[p for p in d['only_tu2'] if 'tu2_only' not in mfile.get(p,set())]
print('residue',len(res),dict(collections.Counter((x['side'],x.get('placed_by','')) for x in res)))
print('name differences explained',ok,'unexplained',bad,badl[:8])
print('files only in main not explained',fo,'| only in tu2 not explained',ft)
