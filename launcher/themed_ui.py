"""Original vector RIG-style interface; no extracted game artwork required."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import settings_model

# Titan Station holo palette: stasis blue on gunmetal, RIG spine green, warning amber.
BG='#050c0c'; PANEL='#0a1615'; EDGE='#1e3d3a'; CYAN='#60e2ce'
TEXT='#dcf4f0'; MUTED='#86a8a3'; AMBER='#ffac46'; RIG='#46e6be'; DIM='#0a1a19'; ALERT='#ff4a3a'
HEAD='Bahnschrift SemiBold Condensed'; FACE='Bahnschrift'; MONO='Consolas'
DLC_DIR=('0000000000000000','454108DF','00000002')

def gui(api):
    root=tk.Tk();root.title('Dead Space 2 • TU2 Alpha')
    icon=Path(__file__).with_name('rig.ico')
    if icon.exists():root.iconbitmap(str(icon))
    root.geometry('1000x740+40+40');root.minsize(940,700);root.configure(bg=BG)
    style=ttk.Style(root);style.theme_use('clam')
    style.configure('RIG.TEntry',fieldbackground='#071219',foreground=TEXT,bordercolor=EDGE,
                    insertcolor=CYAN,padding=6)
    style.configure('RIG.TCombobox',fieldbackground='#071219',background=PANEL,
                    foreground=TEXT,arrowcolor=CYAN,padding=6)
    style.map('RIG.TCombobox',fieldbackground=[('readonly','#071219')],
              foreground=[('readonly',TEXT)],selectbackground=[('readonly',PANEL)])
    style.configure('RIG.Horizontal.TProgressbar',background=RIG,troughcolor=DIM,
                    bordercolor=BG,lightcolor=RIG,darkcolor=RIG,thickness=6)
    style.configure('RIG.TNotebook',background=BG,borderwidth=0)
    style.configure('RIG.TNotebook.Tab',background=PANEL,foreground=MUTED,
                    padding=(22,6),font=(HEAD,12))
    style.map('RIG.TNotebook.Tab',background=[('selected','#19323d')],foreground=[('selected',CYAN)])
    art_file=Path(__file__).with_name('header.png')
    art=tk.PhotoImage(file=str(art_file)) if art_file.exists() else None
    header=tk.Canvas(root,height=art.height() if art else 132,bg=BG,highlightthickness=0)
    header.pack(fill='x');header.art=art
    def draw_header(event=None):
        header.delete('all');w=header.winfo_width();h=int(header['height'])
        if art:header.create_image(0,0,anchor='nw',image=art)
        draw_spine()
        for x,yy,sx,sy in ((w-10,6,-1,1),(w-10,h-8,-1,-1)):
            header.create_line(x,yy+sy*18,x,yy,x+sx*18,yy,fill=CYAN,width=2)
    # RIG spine: live health meter. Required files fill 3 segments each, add-ons and the
    # saves folder 1 each; pulses while busy, flashes red on errors, hover lists what is missing.
    spine={'mode':'idle','phase':0,'tip':None}
    PARTS=(('disc1','Disc 1',3),('update','Title Update 2',3),('disc2','Disc 2 (optional)',1),
           ('dlc','Severed (optional)',1),('user','Saves folder',1))
    def spine_parts():
        try:return [(name,weight,bool(vals[k].get().strip()) or (k=='dlc' and dlc_status.get().startswith('✓')))
                     for k,name,weight in PARTS]
        except (NameError,KeyError):return [(name,weight,False) for _,name,weight in PARTS]
    def mix(a,b,t):
        a=[int(a[i:i+2],16) for i in (1,3,5)];b=[int(b[i:i+2],16) for i in (1,3,5)]
        return '#'+''.join(f'{round(x+(y-x)*t):02x}' for x,y in zip(a,b))
    def draw_spine():
        header.delete('spine')
        parts=spine_parts();level=sum(wt for _,wt,ok in parts if ok)
        required=all(ok for name,_,ok in parts[:2])
        mode,phase=spine['mode'],spine['phase']
        k=int(header['height'])/210  # spine slot coordinates are authored for the 210 px art
        for i in range(9):
            lit=(8-i)<level;y=(30+i*17)*k
            if mode=='error':col=ALERT if (phase%2==0 or phase>8) else '#5a1810'
            elif mode=='busy':col=mix('#1d5c4c',RIG,(1+((phase+i)%6)/5)/2)
            elif lit:col=RIG if required else AMBER
            else:col='#123430'
            header.create_rectangle(34*k,y,50*k,y+11*k,fill=col,outline='',tags='spine')
            if lit and mode not in ('error',):
                header.create_rectangle(32*k,y-2,52*k,y+13*k,outline=mix(col,BG,0.55),tags='spine')
        header.create_rectangle(26*k,22*k,58*k,188*k,outline='',fill='',tags='spine')
    def spine_tick():
        if spine['mode'] in ('busy','error'):
            spine['phase']+=1;draw_spine()
            if spine['mode']=='error' and spine['phase']>14:spine['mode']='idle'
        root.after(110,spine_tick)
    def spine_mode(mode):
        spine['mode']=mode;spine['phase']=0;draw_spine()
    def spine_enter(_):
        lines=['RIG STATUS']+[('✓ ' if ok else '· ')+name for name,_,ok in spine_parts()]
        spine['tip']=header.create_text(70,26,anchor='nw',text='\n'.join(lines),fill=TEXT,
                                        font=(MONO,9),tags='tip')
        x0,y0,x1,y1=header.bbox(spine['tip'])
        header.create_rectangle(x0-8,y0-6,x1+8,y1+6,fill='#081a18',outline=CYAN,tags='tip')
        header.tag_raise(spine['tip'])
    def spine_leave(_):header.delete('tip')
    header.tag_bind('spine','<Enter>',spine_enter);header.tag_bind('spine','<Leave>',spine_leave)
    header.bind('<Configure>',draw_header)
    config=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'DS2TU2Launcher'
    configfile=config/'settings.json'
    verifiedfile=config/'verified.json'
    try:
        saved=json.loads(configfile.read_text()) if configfile.exists() else {}
        if not isinstance(saved,dict):saved={}
    except (OSError,ValueError):saved={}
    vals={};events=queue.Queue();busy=False;game=None
    body=tk.Frame(root,bg=BG);body.pack(fill='both',expand=True,padx=24,pady=(0,16))
    def label(parent,text,fg=MUTED,font=('Segoe UI',10),**kw):
        return tk.Label(parent,text=text,bg=parent['bg'],fg=fg,font=font,**kw)
    def button(parent,text,fn,primary=False):
        return tk.Button(parent,text=text,command=fn,bg=CYAN if primary else '#11272f',
                         fg=BG if primary else TEXT,activebackground='#b4efff' if primary else '#1f4350',
                         activeforeground=BG if primary else TEXT,disabledforeground='#658087',
                         relief='flat',borderwidth=0,padx=14,pady=7,cursor='hand2',
                         font=(HEAD,12),highlightthickness=1,highlightbackground=CYAN if primary else EDGE)
    shell=body
    tabs=ttk.Notebook(shell,style='RIG.TNotebook');tabs.pack(fill='both',expand=True,pady=(0,6))
    body=tk.Frame(tabs,bg=BG);options_page=tk.Frame(tabs,bg=BG)
    tabs.add(body,text='01  /  GAME FILES');tabs.add(options_page,text='02  /  OPTIONS')
    grid=tk.Frame(body,bg=PANEL,highlightthickness=1,highlightbackground=EDGE)
    grid.pack(fill='x');grid.columnconfigure(1,weight=1)
    row_controls=[];dlc_status=tk.StringVar(value='OPTIONAL ADD-ON')
    for row,(key,title,optional) in enumerate([
        ('disc1','Disc 1 ISO',False),('update','Title Update 2',False),
        ('disc2','Disc 2 ISO',True),('dlc','Severed DLC',True)]):
        vals[key]=tk.StringVar(value=saved.get(key,'') if isinstance(saved.get(key,''),str) else '')
        titlebox=tk.Frame(grid,bg=PANEL);titlebox.grid(row=row,column=0,sticky='w',padx=12,pady=3)
        label(titlebox,title,TEXT,(HEAD,13)).pack(anchor='w')
        if key=='dlc':
            dlc_label=tk.Label(titlebox,textvariable=dlc_status,bg=PANEL,fg=AMBER,font=(MONO,8));dlc_label.pack(anchor='w')
        elif optional:label(titlebox,'OPTIONAL ADD-ON',AMBER,(MONO,8)).pack(anchor='w')
        entry=ttk.Entry(grid,textvariable=vals[key],style='RIG.TEntry');entry.grid(row=row,column=1,sticky='ew',padx=8,pady=3)
        def browse(k=key):
            types=[('Disc image or zip','*.iso *.zip'),('All files','*')] if k.startswith('disc') else ([('Update zip or package','*.zip *.xexp'),('All files','*')] if k=='update' else [('DLC zip or package','*.zip'),('All files','*')])
            value=filedialog.askopenfilename(title='Select '+k,filetypes=types)
            if value:vals[k].set(value)
        b=button(grid,'Browse',browse);b.grid(row=row,column=2,padx=(0,8));row_controls.extend([entry,b])
        clear=button(grid,'×',lambda k=key:vals[k].set(''));clear.grid(row=row,column=3,padx=(0,10));row_controls.append(clear)
    label(body,'Zips accepted as downloaded. Discs switch automatically. Disc 2 and Severed are optional; Severed installs on Launch.',AMBER,(FACE,9)).pack(anchor='w',pady=(5,4))
    bottom=tk.Frame(body,bg=BG);bottom.pack(fill='x')
    label(bottom,'LAUNCH DISC',CYAN,(HEAD,12)).pack(side='left')
    vals['active_disc']=tk.StringVar(value='Disc 2' if saved.get('active_disc')==2 and vals['disc2'].get() else 'Disc 1')
    select=ttk.Combobox(bottom,textvariable=vals['active_disc'],values=['Disc 1'],state='readonly',width=11,style='RIG.TCombobox')
    select.pack(side='left',padx=12)
    def update_discs(*_):
        select['values']=['Disc 1','Disc 2'] if vals['disc2'].get().strip() else ['Disc 1']
        if not vals['disc2'].get().strip():vals['active_disc'].set('Disc 1')
    vals['disc2'].trace_add('write',update_discs);update_discs()
    for key,text in [('fullscreen','Fullscreen'),('keyboard','Keyboard / mouse')]:
        vals[key]=tk.BooleanVar(value=saved.get(key,True) if isinstance(saved.get(key,True),bool) else True)
    label(bottom,'F4  in-game settings   ·   F10  exit game',MUTED,(FACE,9)).pack(side='right')
    userdata=tk.Frame(body,bg=BG);userdata.pack(fill='x',pady=(6,4))
    vals['user']=tk.StringVar(value=saved.get('user',str(config/'user')))
    label(userdata,'Saves / content',MUTED,('Segoe UI',9)).pack(side='left',padx=(0,12))
    ue=ttk.Entry(userdata,textvariable=vals['user'],style='RIG.TEntry');ue.pack(side='left',fill='x',expand=True)
    def folder():
        value=filedialog.askdirectory()
        if value:vals['user'].set(value)
    ub=button(userdata,'Browse',folder);ub.pack(side='left',padx=(8,0));row_controls.extend([ue,ub])
    def refresh_dlc(*_):
        try:
            folder_=Path(vals['user'].get()).joinpath(*DLC_DIR)
            done=folder_.is_dir() and any(folder_.iterdir())
        except OSError:done=False
        dlc_status.set('✓ INSTALLED' if done else 'OPTIONAL ADD-ON')
        dlc_label.configure(fg=RIG if done else AMBER)
    vals['user'].trace_add('write',refresh_dlc);refresh_dlc()
    for k in ('disc1','update','disc2','dlc','user'):vals[k].trace_add('write',lambda *_:draw_spine())
    options=tk.Frame(options_page,bg=PANEL,highlightbackground=EDGE,highlightthickness=1)
    options.pack(fill='both',expand=True,pady=(10,0));options.columnconfigure(1,weight=1)
    tk.Frame(options,bg=PANEL,height=6).grid(row=0,column=0)
    option_controls=[];option_by_key={}
    descriptions=[('language','Game language','Español: Spanish text, English audio. Français: French text and audio. Restart the game to apply.'),
                  ('frame_limit','Frame-rate target','30 FPS cap or the experimental 60 FPS mod.'),
                  ('window_size','Window size','Windowed mode only; fullscreen always uses your desktop resolution. Game renders 1280x720, scaled up.'),
                  ('fov','Field of view','Multiplier on the game camera. Original leaves the FOV mod off.')]
    for row,(key,title,hint) in enumerate(descriptions,1):
        value=saved.get(key,settings_model.DEFAULTS[key])
        if value not in settings_model.CHOICES[key]:value=settings_model.DEFAULTS[key]
        vals[key]=tk.StringVar(value=value)
        box=tk.Frame(options,bg=PANEL);box.grid(row=row,column=0,sticky='w',padx=20,pady=4)
        label(box,title,TEXT,(HEAD,13)).pack(anchor='w')
        label(box,hint,MUTED,('Segoe UI',9),wraplength=620,justify='left').pack(anchor='w',pady=(3,0))
        control=ttk.Combobox(options,textvariable=vals[key],values=settings_model.CHOICES[key],state='readonly',width=25,style='RIG.TCombobox')
        control.grid(row=row,column=1,sticky='e',padx=20,pady=4);option_controls.append(control);option_by_key[key]=control
    def sync_window_size(*_):
        option_by_key['window_size'].configure(state='disabled' if busy or game is not None or vals['fullscreen'].get() else 'readonly')
    vals['fullscreen'].trace_add('write',sync_window_size);sync_window_size()
    toggles=tk.Frame(options,bg=PANEL);toggles.grid(row=5,column=0,columnspan=2,sticky='w',padx=16,pady=2)
    for key,title in [('fullscreen','Fullscreen'),('keyboard','Keyboard / mouse controller emulation')]:
        cb=tk.Checkbutton(toggles,text=title,variable=vals[key],bg=PANEL,fg=TEXT,selectcolor=BG,
                          activebackground=PANEL,activeforeground=CYAN,font=('Segoe UI',10))
        cb.pack(side='left',padx=(0,24));row_controls.append(cb)
    label(options,'Applies on the next launch. Saves and shader caches are kept.',AMBER,('Segoe UI',9)).grid(row=6,column=0,sticky='w',padx=20,pady=4)
    def settings_value():
        result={k:v.get() for k,v in vals.items()}
        result['active_disc']=int(result['active_disc'][-1])
        settings_model.validate(result)
        return result
    def save_options():
        try:
            api.atomic_json(configfile,settings_value());status.set('SAVED  /  Options apply on next launch')
        except (OSError,ValueError) as error:messagebox.showerror('Unable to save options',str(error))
    save=button(options,'SAVE OPTIONS',save_options,True);save.grid(row=6,column=1,sticky='e',padx=20,pady=4);row_controls.append(save)
    # Reserve the action dock before the expanding tab content at high DPI.
    body=tk.Frame(shell,bg=BG);body.pack(side='bottom',fill='x',before=tabs)
    status=tk.StringVar(value='STANDBY  /  Select your files to begin')
    label(body,'RIG LOG',CYAN,(HEAD,12)).pack(anchor='w',pady=(0,5))
    output=tk.Text(body,height=2,wrap='word',state='disabled',bg='#03070a',fg='#9fd8c0',
                   font=('Consolas',9),relief='flat',padx=10,pady=7,highlightthickness=1,highlightbackground=EDGE)
    output.pack(fill='both',expand=True)
    progress=ttk.Progressbar(body,mode='indeterminate',style='RIG.Horizontal.TProgressbar')
    progress.pack(fill='x',pady=(4,4))
    footer=tk.Frame(body,bg=BG);footer.pack(fill='x')
    tk.Label(footer,textvariable=status,bg=BG,fg=RIG,font=(MONO,9),anchor='w').pack(side='left',fill='x',expand=True)
    def set_busy(value):
        nonlocal busy
        busy=value
        for b in row_controls+[check]:b.configure(state='disabled' if value or game is not None else 'normal')
        sync_play()
        select.configure(state='disabled' if value or game is not None else 'readonly')
        for control in option_controls:control.configure(state='disabled' if value or game is not None else 'readonly')
        sync_window_size()
        if value:progress.start(12);spine_mode('busy')
        else:
            progress.stop()
            if spine['mode']=='busy':spine_mode('idle')
    # LAUNCH unlocks only after VERIFY FILES has passed for exactly the selected files
    # (path, size and modified time), and stays unlocked across restarts until they change.
    # A pending Severed selection also needs a verify, which installs it.
    def content_signature():
        parts=[]
        for k in ('disc1','update','disc2','user'):
            value=vals[k].get().strip()
            try:
                st=os.stat(value) if value and k!='user' else None
                parts.append([k,value,st.st_size if st else 0,st.st_mtime_ns if st else 0])
            except OSError:parts.append([k,value,-1,-1])
        return json.dumps(parts)
    try:verified=json.loads(verifiedfile.read_text()).get('signature') if verifiedfile.exists() else None
    except (OSError,ValueError,AttributeError):verified=None
    def is_verified():
        return verified is not None and verified==content_signature() and not vals['dlc'].get().strip()
    def sync_play(*_):
        try:play
        except NameError:return
        ok=is_verified()
        play.configure(state='normal' if ok and not busy and game is None else 'disabled')
        if not busy and game is None and not ok and status.get().startswith(('STANDBY','READY')):
            status.set('STANDBY  /  Press VERIFY FILES before your first launch')
    def run(launch):
        nonlocal busy
        if busy or game is not None:return
        if launch and not is_verified():
            messagebox.showinfo('Verify first','Press VERIFY FILES to check your game files before launching.');return
        settings=settings_value();signature=content_signature()
        for key in ('disc1','update','user'):
            if not settings[key].strip():
                messagebox.showerror('Missing file','Please select '+key+'.');return
        set_busy(True);status.set('VERIFYING  /  Checking selected content')
        def worker():
            try:
                api.atomic_json(configfile,settings)
                receipt=api.prepare(settings,lambda s:events.put(('log',s)))
                api.atomic_json(configfile,dict(settings,dlc=''))
                events.put(('ready',signature))
                if launch:
                    proc=subprocess.Popen(api.command(settings,receipt),cwd=api.BASE)
                    events.put(('launched',proc))
            except (Exception,SystemExit) as e:events.put(('error',str(e)))
            finally:events.put(('done',None))
        # Do not terminate an in-progress install when closing the UI.
        threading.Thread(target=worker,daemon=False).start()
    check=button(footer,'VERIFY FILES',lambda:run(False));check.pack(side='left',padx=8)
    play=button(footer,'LAUNCH  ▸',lambda:run(True),True);play.pack(side='right')
    def nonlocal_verified(signature):
        nonlocal verified
        verified=signature
        try:api.atomic_json(verifiedfile,{'signature':signature})
        except OSError:pass
    for k in ('disc1','update','disc2','dlc','user'):vals[k].trace_add('write',sync_play)
    sync_play()
    def append(text):
        output.configure(state='normal');output.insert('end',text+'\n');output.see('end');output.configure(state='disabled')
    def poll():
        nonlocal game
        while not events.empty():
            kind,value=events.get()
            if kind=='log':append(value)
            elif kind=='ready':
                nonlocal_verified(value);vals['dlc'].set('');refresh_dlc();status.set('READY  /  Content verified');sync_play()
            elif kind=='launched':
                game=value;status.set('RUNNING  /  Session active');append('Game started. Close the game to launch again.')
            elif kind=='error':
                spine_mode('error');status.set('HOLD  /  Check the session log');append(value);messagebox.showerror('Unable to continue',value)
            elif kind=='done':set_busy(False)
        if game is not None and game.poll() is not None:
            code=game.returncode;game=None;set_busy(False)
            status.set('SESSION CLOSED' if code==0 else 'HOLD  /  Game exited with an error')
            append('Game exit code: '+str(code))
        root.after(150,poll)
    def close():
        if busy:
            messagebox.showinfo('Operation in progress','Please wait for verification or installation to finish.');return
        root.destroy()
    root.protocol('WM_DELETE_WINDOW',close)
    # Fit the window inside the Windows work area (screen minus taskbar) with its title bar
    # visible; Tk geometry is the client area, so leave room for the frame above it.
    root.update_idletasks()
    try:
        import ctypes
        rect=(ctypes.c_long*4)();ctypes.windll.user32.SystemParametersInfoW(0x30,0,rect,0)  # SPI_GETWORKAREA
        ratio=root.winfo_screenwidth()/max(1,ctypes.windll.user32.GetSystemMetrics(0))
        left,top,right,bottom=(int(v*ratio) for v in rect)
    except (AttributeError,OSError):
        left,top,right,bottom=0,0,root.winfo_screenwidth(),root.winfo_screenheight()-40
    frame=max(32,root.winfo_rooty()-root.winfo_y())+8
    avail_w,avail_h=right-left-16,bottom-top-frame-8
    want_w,want_h=max(1000,root.winfo_reqwidth()),max(720,root.winfo_reqheight()+24)
    if want_h>avail_h or want_w>avail_w:
        root.state('zoomed')
    else:
        root.geometry(f'{want_w}x{want_h}+{left+(avail_w-want_w)//2}+{top+frame}')
    root.minsize(min(want_w,avail_w,900),min(want_h,avail_h,640))
    append('TU2 alpha. Add Disc 2 and Severed only if you want to use them.')
    spine_tick();poll();root.mainloop()
