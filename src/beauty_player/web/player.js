import {BeautyRenderer} from './renderer.js';
import {faceGeometry,validTracking} from './tracking.js';
const $=id=>document.getElementById(id),video=$('video');
let renderer,worker,ready=false,busy=false,tracking=null,generation=0,lastSent=-1,meta,offset=0,subtitleCues=[];
let frameCount=0,frameStart=performance.now(),lastVideoTime=-1,dirty=true,closed=false,lastSeek=0;
let seekSerial=0;
let compatibility=false;
const keys=['smooth','white','slim','eyes'];
const settings={enabled:true,smooth:25,white:10,slim:0,eyes:0};
const notices=new Map();
let toolbar=null,zoom=1;
const frameCallbacks=typeof video.requestVideoFrameCallback==='function';
const time=t=>{t=Math.max(0,Math.floor(t));return `${Math.floor(t/3600).toString().padStart(2,'0')}:${Math.floor(t/60)%60}`.replace(/:(\d)$/ ,':0$1')+':'+(t%60).toString().padStart(2,'0');};
const current=()=>offset+(Number.isFinite(video.currentTime)?video.currentTime:0);
function message(text,error=false,kind='playback'){
  if(text)notices.set(kind,{text,error});else notices.delete(kind);
  const items=[...notices.values()],notice=items.find(n=>n.error)||items[0];
  $('status').textContent=notice?.text||'';$('status').hidden=!notice;
  $('status').classList.toggle('error',Boolean(notice?.error));
  syncBeauty();
}
function syncBeauty(){
  const error=notices.get('engine')?.text||'';
  const status=error?'美颜不可用':!settings.enabled?'美颜已关闭':ready?'实时生效':'正在准备…';
  $('effect-status').textContent=status;$('effect-status').dataset.ready=String(ready&&settings.enabled&&!error);
  if(toolbar&&!toolbar.closed)toolbar.postMessage({type:'beauty-state',settings,status,error,ready:ready&&settings.enabled&&!error},location.origin);
}
function openBeauty(){
  if(toolbar&&!toolbar.closed){toolbar.focus();return;}
  try{
    const left=Math.max(0,Math.min(screen.availWidth-330,window.screenX+window.outerWidth-320));
    toolbar=window.open('toolbar.html',`beauty-${location.pathname}`,`popup=yes,width=300,height=475,left=${left},top=${Math.max(0,window.screenY+75)},resizable=yes`);
  }catch{toolbar=null;}
  if(!toolbar||toolbar.closed){message('美颜工具栏被浏览器拦截，请允许此窗口的弹出式窗口后重试。',true,'toolbar');return;}
  $('beauty-tools').setAttribute('aria-expanded','true');message('',false,'toolbar');toolbar.focus();
}
$('beauty-tools').onclick=openBeauty;
window.addEventListener('message',event=>{
  if(event.origin!==location.origin||event.source!==toolbar)return;
  const data=event.data;
  if(data?.type==='beauty-closed'){$('beauty-tools').setAttribute('aria-expanded','false');return;}
  if(data?.type==='beauty-ready'){syncBeauty();return;}
  if(data?.type==='beauty-change'){
    if(data.key==='enabled'&&typeof data.value==='boolean')settings.enabled=data.value;
    else if(keys.includes(data.key)&&Number.isFinite(data.value))settings[data.key]=Math.round(Math.max(0,Math.min(100,data.value)));
    else return;
  }else if(data?.type==='beauty-preset'&&['natural','reset'].includes(data.preset)){
    const values=data.preset==='natural'?[25,10,0,0]:[0,0,0,0];keys.forEach((key,i)=>settings[key]=values[i]);
  }else return;
  dirty=true;syncBeauty();
});
function setZoom(value){
  zoom=Math.max(.5,Math.min(2,Math.round(value*10)/10));
  $('canvas').style.setProperty('--video-scale',String(zoom));$('zoom-reset').textContent=`${Math.round(zoom*100)}%`;
  $('zoom-out').disabled=zoom===.5;$('zoom-in').disabled=zoom===2;
}
$('zoom-in').onclick=()=>setZoom(zoom+.1);$('zoom-out').onclick=()=>setZoom(zoom-.1);$('zoom-reset').onclick=()=>setZoom(1);
window.addEventListener('pagehide',()=>{
  closed=true;if(toolbar&&!toolbar.closed)toolbar.close();worker?.terminate();video.pause();video.removeAttribute('src');video.load();
});
function invalidate(){generation++;tracking=null;lastSent=-1;dirty=true;}
function setSource(start=0,play=false){
  invalidate();offset=meta.direct&&!compatibility?0:start;
  video.src=meta.direct&&!compatibility?'source':`stream?start=${start.toFixed(6)}&compat=${compatibility?1:0}`;
  video.load();lastSeek=performance.now();
  video.onloadedmetadata=()=>{
    if(!video.videoWidth){if(useCompatibility(start,play))return;message('浏览器没有解码出视频画面，请用原播放器观看。',true);return;}
    if(meta.direct&&!compatibility)video.currentTime=start;
    if(play)video.play().catch(e=>message('请点击播放：'+e.message));
  };
}
function useCompatibility(start,play){
  if(compatibility||meta.codec!=='hevc')return false;
  compatibility=true;document.body.dataset.compatibility='cpu-h264';
  message('正在准备兼容播放…');
  setSource(start,play);return true;
}
async function seek(seconds){
  const next=Math.max(0,Math.min(meta.duration-.1,seconds));
  const playing=!video.paused;
  const serial=++seekSerial;
  invalidate();lastSeek=performance.now();
  if(meta.direct&&!compatibility)video.currentTime=next;
  else try{
    const response=await fetch(`seek?start=${next.toFixed(6)}&compat=${compatibility?1:0}`);
    if(!response.ok)throw Error('未能定位关键帧，请重试或用原播放器观看。');
    const plan=await response.json();
    if(serial===seekSerial)setSource(plan.start,playing);
  }catch(error){message(error.message,true);}
}
async function loadSubtitles(){
  if(!meta.subtitles){$('subtitles').disabled=true;return;}
  const text=await (await fetch('subtitles.vtt')).text();
  const parse=t=>t.split(':').reduce((v,x)=>v*60+Number(x),0);
  for(const block of text.replaceAll('\r','').split(/\n\s*\n/)){
    const lines=block.split('\n'),i=lines.findIndex(l=>l.includes('-->'));
    if(i<0)continue;
    const match=lines[i].match(/([\d:.]+)\s+-->\s+([\d:.]+)/);
    if(match)subtitleCues.push({start:parse(match[1]),end:parse(match[2]),text:lines.slice(i+1).join('\n').replace(/<[^>]*>/g,'')});
  }
}
function draw(){
  if(closed)return;
  const now=performance.now(),t=current();
  if(video.readyState>=2 && renderer){
    const enabled=settings.enabled;
    const valid=validTracking(tracking,generation,t);
    const faces=enabled&&valid?tracking.faces.map(f=>faceGeometry(f,video.videoWidth/video.videoHeight)).filter(Boolean):[];
    if((!frameCallbacks && lastVideoTime!==video.currentTime) || dirty){
      renderer.draw(video,video.videoWidth,video.videoHeight,faces,enabled?keys.map(k=>settings[k]/100):[0,0,0,0]);
      lastVideoTime=video.currentTime;dirty=false;frameCount++;
    }
    if(ready&&!busy&&enabled&&Math.abs(t-lastSent)>.085&&now-lastSeek>120){
      busy=true;lastSent=t;const captureGeneration=generation;
      createImageBitmap(video,{resizeWidth:Math.min(512,video.videoWidth),resizeHeight:Math.round(Math.min(512,video.videoWidth)*video.videoHeight/video.videoWidth)}).then(bitmap=>{
        if(closed){bitmap.close();busy=false;return;}
        worker.postMessage({bitmap,time:t,generation:captureGeneration,stamp:performance.now()},[bitmap]);
      }).catch(()=>{busy=false;});
    }
    if(document.activeElement!==$('seek'))$('seek').value=t;
    $('time').textContent=`${time(t)} / ${time(meta.duration)}`;
    $('subtitle').textContent=$('subtitles').checked?subtitleCues.filter(c=>c.start<=t&&t<c.end).map(c=>c.text).join('\n'):'';
  }
  if(now-frameStart>1500){
    const fps=frameCount*1000/(now-frameStart);frameCount=0;frameStart=now;
    document.body.dataset.fps=fps.toFixed(1);
  }
  requestAnimationFrame(draw);
}
async function start(){
  try{
    meta=await(await fetch('metadata')).json();$('title').textContent=meta.title;$('seek').max=meta.duration;
    renderer=new BeautyRenderer($('canvas'));
    worker=new Worker(new URL('./tracking-worker.js',import.meta.url));
    worker.onmessage=e=>{
      const result=e.data;
      if(result.type==='ready'){ready=true;document.body.dataset.engine='ready';message('',false,'engine');}
      if(result.type==='faces'){busy=false;if(result.generation===generation){tracking=result;dirty=true;document.body.dataset.faces=String(result.faces.length);}}
      if(result.type==='error'){busy=false;ready=false;tracking=null;dirty=true;message('美颜组件加载失败，当前播放原画。请重新打开播放器。',true,'engine');document.body.dataset.engine='error';}
    };
    worker.onerror=()=>{ready=false;busy=false;tracking=null;dirty=true;message('美颜组件无法运行，当前播放原画。请重新打开播放器。',true,'engine');document.body.dataset.engine='error';};
    video.volume=.8;
    if(frameCallbacks){const onFrame=()=>{dirty=true;if(!closed)video.requestVideoFrameCallback(onFrame);};video.requestVideoFrameCallback(onFrame);}
    video.addEventListener('loadedmetadata',()=>{$('play').disabled=!video.videoWidth;$('empty').textContent='点击播放';});
    video.onloadeddata=()=>{
      if(!video.videoWidth)return;
      $('play').disabled=false;$('empty').hidden=true;dirty=true;
      message('');
    };
    video.onerror=()=>{if(useCompatibility(current(),!video.paused))return;message('浏览器无法解码此视频或音轨。请用原播放器观看；原片未修改。',true);$('empty').textContent='此格式暂时无法在美颜播放器中播放';$('empty').hidden=false;};
    video.onplay=()=>{$('play').textContent='暂停';};video.onpause=()=>{$('play').textContent='播放';};
    video.onseeking=invalidate;video.onseeked=()=>{dirty=true;};
    $('play').onclick=()=>video.paused?video.play().catch(e=>message(e.message,true)):video.pause();
    $('back').onclick=()=>seek(current()-10);$('forward').onclick=()=>seek(current()+10);
    $('seek').onchange=()=>seek(Number($('seek').value));$('volume').oninput=()=>{video.volume=Number($('volume').value);};
    $('fullscreen').onclick=()=>{
      const action=document.fullscreenElement?document.exitFullscreen():$('viewer').requestFullscreen();
      action.catch(()=>message('未能进入或退出全屏，请重试。',true));
    };
    document.addEventListener('keydown',e=>{if(['INPUT','BUTTON'].includes(e.target.tagName))return;if(e.code==='Space'){e.preventDefault();$('play').click();}if(e.code==='ArrowLeft')seek(current()-10);if(e.code==='ArrowRight')seek(current()+10);if(e.code==='Escape'&&document.fullscreenElement)document.exitFullscreen();});
    $('canvas').addEventListener('webglcontextlost',e=>{e.preventDefault();ready=false;video.pause();message('画面显示中断，请重新打开美颜播放器。',true,'engine');document.body.dataset.engine='error';});
    setSource();loadSubtitles().catch(()=>message('中文字幕未能载入，视频仍可播放。',true,'subtitles'));draw();
    const ping=setInterval(()=>fetch('ping').catch(()=>{}),15000);
    window.addEventListener('pagehide',()=>clearInterval(ping));
  }catch(error){message(error.message,true,'engine');document.body.dataset.engine='error';}
}
start();
