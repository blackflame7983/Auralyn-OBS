// 隔離OBS専用の制御。認証値は設定ファイルからメモリ内だけへ読み、出力しない。
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {createInterface} from 'node:readline';
const settings=JSON.parse(readFileSync(process.argv[2],'utf8'));
const socket=new WebSocket(`ws://127.0.0.1:${settings.server_port}`);
const output=value=>process.stdout.write(JSON.stringify(value)+'\n');
socket.addEventListener('message',event=>{
    try {
        const message=JSON.parse(String(event.data));
        if(message.op===0){
            const {salt,challenge}=message.d.authentication;
            const secret=createHash('sha256').update(settings.server_password+salt).digest('base64');
            const authentication=createHash('sha256').update(secret+challenge).digest('base64');
            socket.send(JSON.stringify({op:1,d:{rpcVersion:1,authentication,eventSubscriptions:65536}}));
        } else if(message.op===2) output({ready:true});
        else if(message.op===7) output({response:message.d});
        else if(message.op===5&&message.d.eventType==='InputVolumeMeters') output({meters:message.d.eventData.inputs});
    } catch { output({error:'OBS message could not be decoded'}); }
});
socket.addEventListener('error',()=>output({error:'OBS WebSocket connection failed'}));
createInterface({input:process.stdin}).on('line',line=>{
    try { const command=JSON.parse(line); socket.send(JSON.stringify({op:6,d:command})); }
    catch { output({error:'Invalid request or disconnected OBS'}); }
});
process.stdin.on('end',()=>{socket.close();setTimeout(()=>process.exit(0),100);});
