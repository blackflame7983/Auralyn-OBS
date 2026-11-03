"""実engine/隔離OBSと、読み取り専用の参照PCMを扱う。通常設定は書き換えない。"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import numpy as np


def runtime_env():
    names={'PATH','SYSTEMROOT','SYSTEMDRIVE','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','USERPROFILE',
           'APPDATA','LOCALAPPDATA','PROGRAMDATA','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432','ALLUSERSPROFILE'}
    return {k:v for k,v in os.environ.items() if k.upper() in names}


def snapshot(root):
    result={}
    if root.exists():
        for p in root.rglob('*'):
            if p.is_file(): result[str(p.relative_to(root))]=hashlib.sha256(p.read_bytes()).hexdigest()
    return result


class Engine:
    def __init__(self, binary, session, endpoint):
        env=runtime_env()
        for key,name in [('APPDATA','roaming'),('LOCALAPPDATA','local')]:
            folder=session/name; folder.mkdir(exist_ok=True); env[key]=str(folder)
        env['AURALYN_OBS_POC_ENDPOINT']=endpoint
        self.responses=queue.Queue(); self.levels={'input':0.0,'output':0.0,'events':0}; self.errors=[]
        self.log=(session/'engine.stderr.log').open('wb')
        self.process=subprocess.Popen([str(binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,
                                      env=env,cwd=binary.parent)
        self.thread=threading.Thread(target=self.read,daemon=True); self.thread.start()
    def read(self):
        for raw in self.process.stdout:
            try:
                line=raw.decode('utf-8').strip()
                if not line.startswith('IPC:'): continue
                message=json.loads(line[4:]); payload=message.get('data',{})
                if message.get('kind')=='Response': self.responses.put(payload)
                elif message.get('kind')=='Event':
                    if payload.get('type')=='LevelMeter':
                        levels=payload['payload']; self.levels['events']+=1
                        self.levels['input']=max(self.levels['input'],*levels['input'])
                        self.levels['output']=max(self.levels['output'],*levels['output'])
                    elif payload.get('type')=='Error': self.errors.append(payload.get('payload'))
            except (ValueError,KeyError,UnicodeError): self.errors.append('IPC decode failed')
    def request(self,kind,payload=None,timeout=30):
        data={'type':kind}
        if payload is not None: data['payload']=payload
        self.process.stdin.write((json.dumps(data)+'\n').encode()); self.process.stdin.flush()
        try: reply=self.responses.get(timeout=timeout)
        except queue.Empty: raise RuntimeError(f'Engine {kind} timeout; see engine.stderr.log') from None
        if reply.get('type')=='Error': raise RuntimeError(f'Engine {kind} rejected: {reply.get("payload")}')
        return reply
    def close(self):
        if self.process.poll() is None:
            try: self.request('Stop',timeout=10)
            except (RuntimeError,OSError): pass
            self.process.stdin.close()
            try: self.process.wait(timeout=10)
            except subprocess.TimeoutExpired: self.process.terminate(); self.process.wait(timeout=5)
        self.log.close()
    def devices(self):
        token=self.request('BeginDeviceScan')['payload']['token']
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            value=self.request('PollDeviceScan',{'token':token})['payload']['status']
            if value['status']=='Completed': return value['data']
            if value['status'] not in ('Pending','pending'): raise RuntimeError('Device scan failed')
            time.sleep(.1)
        raise RuntimeError('Device scan timed out')


class Obs:
    def __init__(self,client,config,session):
        self.responses=queue.Queue(); self.ready=threading.Event(); self.meters={}; self.serial=0
        self.log=(session/'obs-client.stderr.log').open('wb')
        self.process=subprocess.Popen(['node',str(client),str(config)],env=runtime_env(),stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE,stderr=self.log)
        self.thread=threading.Thread(target=self.read,daemon=True); self.thread.start()
    def read(self):
        for raw in self.process.stdout:
            try:
                data=json.loads(raw)
                if data.get('ready'): self.ready.set()
                if 'response' in data: self.responses.put(data['response'])
                if 'meters' in data:
                    for source in data['meters']:
                        values=source.get('inputLevelsMul',[])
                        self.meters[source['inputName']]=max([x for row in values for x in row],default=0)
            except (ValueError,KeyError): pass
    def request(self,kind,data=None,timeout=10):
        self.serial+=1
        self.process.stdin.write((json.dumps({'requestType':kind,'requestId':str(self.serial),'requestData':data or {}})+'\n').encode()); self.process.stdin.flush()
        try: reply=self.responses.get(timeout=timeout)
        except queue.Empty: raise RuntimeError(f'OBS {kind} timeout') from None
        if not reply['requestStatus']['result']: raise RuntimeError(f'OBS {kind} failed {reply["requestStatus"]["code"]}: {reply["requestStatus"].get("comment", "")}')
        return reply.get('responseData',{})
    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.terminate(); self.process.wait(timeout=5)
        self.log.close()


class Reference:
    def __init__(self,endpoint,path):
        self.path=path; self.frames=0; self.dropped=0; self.error=None; self.marks=[]; self.stop=threading.Event(); self.ready=threading.Event()
        self.endpoint=endpoint; self.thread=threading.Thread(target=self.run,daemon=True); self.thread.start()
    def run(self):
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenFileMappingW.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR]; kernel.OpenFileMappingW.restype=wintypes.HANDLE
        kernel.MapViewOfFile.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.c_size_t]; kernel.MapViewOfFile.restype=ctypes.c_void_p
        kernel.UnmapViewOfFile.argtypes=[ctypes.c_void_p]; kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=None; pointer=None
        try:
            deadline=time.monotonic()+15
            while not handle and time.monotonic()<deadline:
                handle=kernel.OpenFileMappingW(4,False,'Local\\Auralyn.OBS.PoC.v1.'+self.endpoint)
                if not handle: time.sleep(.02)
            if not handle: raise RuntimeError('Reference mapping unavailable')
            pointer=kernel.MapViewOfFile(handle,4,0,0,196672)
            if not pointer: raise RuntimeError('Reference map failed')
            data=np.ctypeslib.as_array((ctypes.c_uint32*49168).from_address(pointer))
            from protocol_contract import valid_stream
            if (list(data[:2]) != [0x41555043, 1] or list(data[3:6]) != [196672, 16384, 2]
                    or not valid_stream(int(data[2]), int(data[6]))):
                raise RuntimeError('Reference protocol mismatch')
            self.rate=int(data[6]); self.minor=int(data[2]); cursor=int(data[10]); self.first_frame=cursor; self.ready.set()
            last_mark=0
            with self.path.open('xb') as output:
                while not self.stop.is_set():
                    if int(data[6]) != self.rate or int(data[2]) != self.minor:
                        raise RuntimeError('Reference rate/minor changed during capture')
                    write=int(data[10]); available=(write-cursor)&0xffffffff
                    if available>16384: self.dropped+=available; cursor=write; continue
                    if available:
                        frames=(np.arange(available,dtype=np.uint32)+np.uint32(cursor))
                        indices=16+(frames&16383)*3
                        slots=np.stack((data[indices].copy(),data[indices+1].copy(),data[indices+2].copy()),axis=1)
                        tags_after=data[indices].copy(); expected=frames<<1
                        if not np.all((slots[:,0]==expected)&(tags_after==expected)): self.dropped+=available
                        else: output.write(slots[:,1:].astype('<u4',copy=False).tobytes()); self.frames+=available
                        cursor=write
                        now=time.perf_counter_ns()
                        if now-last_mark>=1000000000:
                            self.marks.append({'perfNs':now,'sampleFrame':self.frames,'producerFrame':write}); last_mark=now
                    time.sleep(.003)
        except Exception as error:
            self.error=type(error).__name__+': '+str(error); self.ready.set()
        finally:
            if pointer: kernel.UnmapViewOfFile(pointer)
            if handle: kernel.CloseHandle(handle)
    def close(self):
        self.stop.set(); self.thread.join(timeout=10)
        return {'frames':self.frames,'dropped':self.dropped,'rate':getattr(self,'rate',0),'streamMinor':getattr(self,'minor',None),'firstFrame':getattr(self,'first_frame',0),'error':self.error,'marks':self.marks}
