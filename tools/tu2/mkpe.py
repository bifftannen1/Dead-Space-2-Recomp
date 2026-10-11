import struct,glob,sys
base=open(sys.argv[1],'rb').read()
pe=struct.unpack('<I',base[0x3C:0x40])[0]; nsec=struct.unpack('<H',base[pe+6:pe+8])[0]; opt=struct.unpack('<H',base[pe+20:pe+22])[0]
ib=struct.unpack('<I',base[pe+24+28:pe+24+32])[0]; st=pe+24+opt
old=[]
for i in range(nsec):
    s=base[st+i*40:st+i*40+40]; old.append((s[:8],)+struct.unpack('<IIII',s[8:24])+(s[24:40],))
dumps=sorted((int(f.split('.')[1],16),open(f,'rb').read()) for f in glob.glob('img/tu2.*.bin'))
assert len(dumps)==nsec-1  # .reloc not dumped
hdr=bytearray(base[:0x400]); body=bytearray(); off=0x400; secs=[]
for (name,vs,va,rs,ro,tail),(nva,data) in zip(old[:-1],dumps):
    raw=(len(data)+0x1FF)&~0x1FF
    secs.append((name,len(data),nva-ib,raw,off,tail,va,vs)); body+=data+b'\0'*(raw-len(data)); off+=raw
struct.pack_into('<H',hdr,pe+6,len(secs))
for i,(name,vs,va,raw,ro,tail,_,_) in enumerate(secs):
    struct.pack_into('<8sIIII',hdr,st+i*40,name,vs,va,raw,ro); hdr[st+i*40+24:st+i*40+40]=tail
hdr[st+len(secs)*40:st+nsec*40]=b'\0'*40
def remap(rva):
    for name,vs,va,raw,ro,tail,ova,ovs in secs:
        if ova<=rva<ova+ovs: return rva-ova+va
    return 0
dd=pe+24+96
for k in range(16):
    r,sz=struct.unpack('<II',hdr[dd+k*8:dd+k*8+8])
    if r:
        nr=remap(r); struct.pack_into('<I',hdr,dd+k*8,nr)
        if k==3:  # exception dir size: follow .pdata size
            struct.pack_into('<I',hdr,dd+k*8+4,[s for s in secs if s[0].startswith(b'.pdata')][0][1])
        if k==5: struct.pack_into('<II',hdr,dd+k*8,0,0)  # relocs dropped
last=secs[-1]; struct.pack_into('<I',hdr,pe+24+56,(last[2]+last[1]+0xFFF)&~0xFFF)
# entry point: take from XEX header via rexglue? keep: remap old entry
ep=struct.unpack('<I',hdr[pe+24+16:pe+24+20])[0]; struct.pack_into('<I',hdr,pe+24+16,remap(ep))
open(sys.argv[2],'wb').write(bytes(hdr)+bytes(body)); print('PE written',len(hdr)+len(body),'sections',len(secs))
