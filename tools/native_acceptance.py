"""USBマイク/実VSTから隔離OBS録画へ送る。配信・通常設定変更・物理出力はしない。"""
import argparse
import ctypes
import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from native_support import Engine, Obs, Reference, runtime_env, snapshot

ROOT=Path(__file__).resolve().parents[1]
NAME='Auralyn PCM #223'


def prepare(plugin_package=None):
    session=ROOT/'build'/('native-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    session.mkdir(exist_ok=False)
    portable=session/'obs-portable'; portable.mkdir()
    for name in ['bin','data','obs-plugins']:
        shutil.copytree(Path('C:/Program Files/obs-studio')/name,portable/name)
    if plugin_package is None:
        shutil.copy2(ROOT/'build/cmake/Release/auralyn-obs.dll',portable/'obs-plugins/64bit/auralyn-obs.dll')
    (portable/'portable_mode.txt').write_text('')
    if plugin_package is not None:
        from manage_install import apply
        (portable/'.auralyn-obs-isolated-test').write_text('Auralyn OBS isolated portable test only\n',encoding='utf-8')
        apply(portable.resolve(),'install',plugin_package.resolve(),None)
    config=portable/'config/obs-studio'; profile=config/'basic/profiles/Native223'; profile.mkdir(parents=True)
    scenes=config/'basic/scenes'; scenes.mkdir(parents=True)
    recordings=session/'recordings'; recordings.mkdir()
    telemetry=session/'telemetry'; telemetry.mkdir()
    (config/'user.ini').write_text('[Basic]\nSceneCollection=Native223\nSceneCollectionFile=Native223\nProfile=Native223\nProfileDir=Native223\nConfigOnNewProfile=false\n[General]\nFirstRun=true\nConfirmOnExit=false\n',encoding='utf-8')
    (config/'global.ini').write_text('[General]\nFirstRun=true\nConfirmOnExit=false\n',encoding='utf-8')
    (profile/'basic.ini').write_text('[General]\nName=Native223\n[Video]\nBaseCX=320\nBaseCY=180\nOutputCX=320\nOutputCY=180\nFPSType=0\nFPSCommon=10\n[Audio]\nSampleRate=48000\nChannelSetup=Stereo\n[Output]\nMode=Simple\n[SimpleOutput]\nRecQuality=Lossless\nFilePath='+recordings.as_posix()+'\nRecFormat2=avi\n[AdvOut]\nTrackIndex=1\n',encoding='utf-8')
    (scenes/'Native223.json').write_text(json.dumps({'name':'Native223','current_scene':'Native223','current_program_scene':'Native223','scene_order':[{'name':'Native223'}],'groups':[],
        'sources':[{'name':'Native223','uuid':str(uuid.uuid4()),'id':'scene','versioned_id':'scene','settings':{'items':[]},'mixers':0}]}),encoding='utf-8')
    with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    ws=config/'plugin_config/obs-websocket/config.json'; ws.parent.mkdir(parents=True)
    ws.write_text(json.dumps({'first_load':False,'server_enabled':True,'server_port':port,'auth_required':True,'server_password':secrets.token_urlsafe(32),'alerts_enabled':False}),encoding='utf-8')
    normal=Path(os.environ['APPDATA'])/'obs-studio'
    before=snapshot(normal); (session/'normal-obs-before.json').write_text(json.dumps(before),encoding='utf-8')
    env=runtime_env(); env['AURALYN_OBS_POC_TELEMETRY_DIR']=str(telemetry)
    startup=subprocess.STARTUPINFO(); startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow=0
    with (session/'obs-process.log').open('wb') as log:
        child=subprocess.Popen([str(portable/'bin/64bit/obs64.exe'),'--portable','--multi','--disable-updater','--disable-shutdown-check','--websocket_ipv4_only'],
            cwd=portable/'bin/64bit',env=env,stdout=log,stderr=subprocess.STDOUT,startupinfo=startup)
    meta={'session':str(session),'obsPid':child.pid,'wsConfig':str(ws),'normalObsConfig':str(normal),'endpoint':'native223-'+str(child.pid)}
    (session/'session.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
    print(json.dumps({'prepared':str(session),'obsPid':child.pid}),flush=True)
    return session


def connect(session):
    meta=json.loads((session/'session.json').read_text()); deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        obs=Obs(ROOT/'tools/obs_client.mjs',Path(meta['wsConfig']),session)
        if obs.ready.wait(3): return obs,meta
        obs.close(); time.sleep(.2)
    raise RuntimeError('Isolated OBS did not become ready')


def run(session,args):
    obs,meta=connect(session)
    evidence=session/'runs'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S'); evidence.mkdir(parents=True,exist_ok=False)
    engine=None; reference=None; recording=False
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    result={'durationRequestedSeconds':args.duration,'route':'actual microphone -> actual VST -> shared memory -> OBS AVI PCM recording','startedUtc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        assert not obs.request('GetStreamStatus')['outputActive']
        assert not obs.request('GetRecordStatus')['outputActive']
        inputs=obs.request('GetInputList')['inputs']
        if not any(x['inputName']==NAME for x in inputs):
            obs.request('CreateInput',{'sceneName':'Native223','inputName':NAME,'inputKind':'auralyn_pcm_poc_v1','inputSettings':{'endpoint':meta['endpoint']},'sceneItemEnabled':True})
        obs.request('SetInputAudioMonitorType',{'inputName':NAME,'monitorType':'OBS_MONITORING_TYPE_NONE'})
        obs.request('SetInputAudioSyncOffset',{'inputName':NAME,'inputAudioSyncOffset':0})
        engine_session=evidence/'engine'; engine_session.mkdir(exist_ok=True)
        engine=Engine(args.auralyn/'src-tauri/target/debug/audio_engine.exe',engine_session,meta['endpoint'])
        rows=engine.devices()
        # 入力IDは現行の機器一覧と一致させる。番号付き表示名の変化で別マイクへ替えない。
        selected=json.loads(args.device.read_text(encoding='utf-8'))
        mic=next(d for d in rows if d['id']==selected['id'] and d['is_input'])
        output=next(d for d in rows if d['host']=='Wasapi' and not d['is_input'] and args.output in d['name'])
        engine.request('LiveOutput',{'config':{'deviceId':None,'monitorTrim':0,'broadcastTrim':1,'monitorMuted':True}})
        engine.request('SetNoiseReduction',{'active':False,'mode':None})
        plugin=engine.request('LoadPlugin',{'path':str(args.vst)})
        started=engine.request('Start',{'host':'Wasapi','input':mic['name'],'output':output['name'],'input_id':mic['id'],'output_id':output['id'],
            'buffer_size':512,'sample_rate':48000,'channel_routing':{'inputChannels':[0,0],'outputRouting':{'mode':'independent','channels':[0,1]}}})
        time.sleep(2)
        baseline=engine.request('GetRuntimeStats')['payload']
        assert baseline['enabled_plugin_count']==1 and not baseline['global_bypass']
        result.update(input=mic['name'],outputClock=output['name'],physicalMonitorMuted=True,plugin=plugin['payload'],started=started['payload'],baseline=baseline)
        telemetry_file=session/'telemetry'/(meta['endpoint']+'.json')
        result['baselineBridge']=json.loads(telemetry_file.read_text()) if telemetry_file.exists() else {}
        reference=Reference(meta['endpoint'],evidence/'reference.f32')
        if not reference.ready.wait(15) or reference.error: raise RuntimeError('Reference capture unavailable')
        obs.request('StartRecord'); recording=True
        start_deadline=time.monotonic()+10
        while not obs.request('GetRecordStatus')['outputActive']:
            if time.monotonic()>start_deadline: raise RuntimeError('OBS recording did not start')
            time.sleep(.1)
        begin=time.monotonic(); end=begin+args.duration; next_log=begin
        result['recordingStartedUtc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        (evidence/'run.json').write_text(json.dumps({'duration':args.duration,'recordingStartedUtc':result['recordingStartedUtc'],'session':str(session)}),encoding='utf-8')
        print(json.dumps({'recording':True,'session':str(session),'evidence':str(evidence),'input':mic['name'],'vst':plugin['payload']['name'],'seconds':args.duration},ensure_ascii=True),flush=True)
        with (evidence/'timeline.jsonl').open('w',encoding='utf-8') as timeline:
            while time.monotonic()<end:
                if (evidence/'stop.request').exists(): result['interrupted']=True; break
                now=time.monotonic()
                if now>=next_log:
                    status=obs.request('GetRecordStatus'); stats=engine.request('GetRuntimeStats')['payload']
                    latest=session/'telemetry'/(meta['endpoint']+'.json')
                    bridge=json.loads(latest.read_text()) if latest.exists() else {}
                    row={'elapsedSeconds':round(now-begin,3),'record':status,'bridge':bridge,'engine':stats,'levels':dict(engine.levels),
                         'obsMeter':obs.meters.get(NAME,0),'referenceFrames':reference.frames,'referenceDropped':reference.dropped}
                    timeline.write(json.dumps(row)+'\n'); timeline.flush()
                    (evidence/'status.json').write_text(json.dumps(row),encoding='utf-8')
                    print(json.dumps({'elapsed':round(now-begin,1),'recording':status['outputActive'],'queue':bridge.get('queued'),'ppm':bridge.get('correctionPpm'),
                        'underruns':bridge.get('underruns'),'overruns':bridge.get('overruns'),'micPeak':engine.levels['input'],'obsMeter':obs.meters.get(NAME,0),'referenceDropped':reference.dropped}),flush=True)
                    if not status['outputActive'] or reference.error or engine.process.poll() is not None: raise RuntimeError('Native acceptance stopped unexpectedly')
                    next_log=now+10
                time.sleep(.1)
        result['elapsedSeconds']=time.monotonic()-begin
        result['recording']=obs.request('StopRecord'); recording=False
        result['finalEngine']=engine.request('GetRuntimeStats')['payload']; result['levels']=engine.levels
        result['finalBridge']=json.loads(telemetry_file.read_text())
        result['reference']=reference.close(); reference=None
        result['normalObsPreserved']=snapshot(Path(meta['normalObsConfig']))==json.loads((session/'normal-obs-before.json').read_text())
        (evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps({'completedSeconds':result['elapsedSeconds'],'recordingPath':result['recording'].get('outputPath'),'normalObsPreserved':result['normalObsPreserved']}),flush=True)
    finally:
        if recording:
            try: obs.request('StopRecord')
            except RuntimeError: pass
        if reference: reference.close()
        if engine: engine.close()
        obs.close()
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',type=Path)
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--plugin-package',type=Path,help='Install a reviewed local package into the new isolated OBS before launching')
    parser.add_argument('--duration',type=int,default=60)
    parser.add_argument('--auralyn',type=Path,default=ROOT.parent/'VSTHost-obs223')
    parser.add_argument('--device',type=Path,default=ROOT.parent/'VSTHost-obs223/test-results/issue223/native-camera/camera-device.json')
    parser.add_argument('--vst',type=Path,default=Path('C:/Program Files/Common Files/VST3/OTT.vst3'))
    parser.add_argument('--output',default='Soundblaster',help='Explicit physical output clock name; its monitor remains muted')
    args=parser.parse_args()
    if not 10<=args.duration<=7200: parser.error('duration must be 10..7200 seconds')
    session=args.session or prepare(args.plugin_package)
    if not args.prepare_only: run(session,args)


if __name__=='__main__':main()
