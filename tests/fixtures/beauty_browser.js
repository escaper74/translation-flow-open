// Opt-in local integration fixture. Never served by the production handler.
import {BeautyRenderer} from './renderer.js';
const $=id=>document.getElementById(id),v=$('video');
const result={checks:{},errors:[]};
let toolbar;
const nativeOpen=window.open;
window.open=function(...args){toolbar=nativeOpen.apply(this,args);return toolbar;};
const control=id=>toolbar.document.getElementById(id);
let captureNext=null;
const originalDraw=BeautyRenderer.prototype.draw;
BeautyRenderer.prototype.draw=function(...args){
  originalDraw.apply(this,args);
  if(!captureNext)return;
  // Read inside the real draw, before WebGL discards its presentation buffer.
  const c=document.createElement('canvas');c.width=$('canvas').width;c.height=$('canvas').height;
  const ctx=c.getContext('2d');ctx.drawImage($('canvas'),0,0);
  const resolve=captureNext;captureNext=null;
  resolve({data:ctx.getImageData(0,0,c.width,c.height).data,png:c.toDataURL('image/png')});
};
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,ms=30000){const end=performance.now()+ms;while(!fn()){if(performance.now()>end)throw Error('Timed out: '+fn);await wait(100);}}
function check(name,condition){result.checks[name]=Boolean(condition);if(!condition)throw Error(name);}
function slider(id,value){control(id).value=value;control(id).dispatchEvent(new Event('input',{bubbles:true}));}
async function pixels(){
  await wait(100); // Let preset replies settle before requesting a redraw.
  const frame=new Promise(resolve=>captureNext=resolve);
  slider('smooth',control('smooth').value);
  return Promise.race([frame,wait(5000).then(()=>{throw Error('No beauty redraw');})]);
}
function difference(a,b){let total=0;for(let i=0;i<a.length;i+=4)total+=Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])+Math.abs(a[i+2]-b[i+2]);return total/(a.length/4);}
try{
  await until(()=>['ready','error'].includes(document.body.dataset.engine));
  check('local_model_ready',document.body.dataset.engine==='ready');
  await until(()=>!$('play').disabled);
  document.body.dataset.testPhase='open-tools';
  await until(()=>toolbar&&!toolbar.closed&&toolbar.document.getElementById('beauty-controls')&&!control('beauty-controls').disabled);
  check('toolbar_separate_window',toolbar!==window&&toolbar.opener===window&&!document.getElementById('smooth'));
  check('simple_status',control('effect-status').textContent==='实时生效'&&!document.getElementById('tracking')&&!document.getElementById('performance'));
  $('play').click();
  await until(()=>v.currentTime>.5&&document.body.dataset.faces==='1');
  result.video={width:v.videoWidth,height:v.videoHeight,currentTime:v.currentTime};
  check('video_decoded',v.videoWidth>0&&v.videoHeight>0);
  check('face_detected',document.body.dataset.faces==='1');
  await wait(3000);
  result.fps=Number(document.body.dataset.fps);
  const quality=v.getVideoPlaybackQuality();
  result.playbackQuality={total:quality.totalVideoFrames,dropped:quality.droppedVideoFrames};
  $('play').click();await until(()=>v.paused);
  control('reset').click();
  const before=await pixels();
  result.sliderDifferences={};
  for(const key of ['smooth','white','slim','eyes']){
    control('reset').click();slider(key,100);
    result.sliderDifferences[key]=difference(before.data,(await pixels()).data);
    check(key+'_changes_pixels',result.sliderDifferences[key]>.001);
  }
  slider('white',100);slider('smooth',90);slider('slim',100);slider('eyes',100);
  const after=await pixels();
  result.pixelDifference=difference(before.data,after.data);
  check('paused_sliders_change_pixels',result.pixelDifference>.01);
  control('enabled').checked=false;control('enabled').dispatchEvent(new Event('change'));
  const disabled=await pixels();
  check('disable_restores_original',difference(before.data,disabled.data)<.01);
  control('enabled').checked=true;control('enabled').dispatchEvent(new Event('change'));
  slider('smooth',63);slider('white',27);slider('slim',14);slider('eyes',18);await wait(150);
  const pausedTime=v.currentTime;
  control('close-tools').click();await until(()=>toolbar.closed);
  await until(()=>$('beauty-tools').getAttribute('aria-expanded')==='false');
  check('closing_toolbar_preserves_pause',v.paused&&v.currentTime===pausedTime);
  $('play').click();await until(()=>!v.paused&&v.currentTime>pausedTime+.2);
  check('playback_without_toolbar',true);
  // Explicitly exercise a denied popup without changing browser preferences.
  window.open=()=>null;$('beauty-tools').click();
  check('blocked_popup_explicit',$('status').textContent.includes('拦截')&&!$('status').hidden);
  window.open=function(...args){toolbar=nativeOpen.apply(this,args);return toolbar;};
  document.body.dataset.testPhase='reopen-tools';
  await until(()=>toolbar&&!toolbar.closed&&toolbar.document.getElementById('beauty-controls')&&!control('beauty-controls').disabled);
  check('reopen_keeps_strength',control('smooth').value==='63'&&control('white').value==='27'&&control('slim').value==='14'&&control('eyes').value==='18'&&control('enabled').checked);
  check('reopen_clears_popup_error',$('status').hidden);
  $('play').click();await until(()=>v.paused);
  const reopened=await pixels();slider('white',100);const readjusted=await pixels();
  check('reopened_toolbar_changes_pixels',difference(reopened.data,readjusted.data)>.01);
  control('natural').click();
  result.compatibility=document.body.dataset.compatibility||'native';
  const seekPlan=await(await fetch('seek?start=12&compat='+(result.compatibility==='native'?0:1))).json();
  $('seek').value=12;$('seek').dispatchEvent(new Event('change'));
  const expected='00:00:'+Math.floor(seekPlan.start).toString().padStart(2,'0');
  await until(()=>$('time').textContent.startsWith(expected)&&$('subtitle').textContent.includes(seekPlan.start<10?'本地播放':'拖动'));
  result.seekAnchor=seekPlan.start;
  check('seek_and_subtitle',true);
  $('subtitles').checked=false;
  await until(()=>$('subtitle').textContent==='');check('subtitle_toggle',true);
  $('subtitles').checked=true;
  $('play').click();await until(()=>!v.paused&&v.currentTime>.2);
  await wait(1000);$('play').click();
  result.image=after.png;
  check('resume_after_seek',true);
  $('volume').value=.37;$('volume').dispatchEvent(new Event('input'));
  check('volume_preserved',Math.abs(v.volume-.37)<.001);
  document.body.dataset.testPhase='window-checks';
  await until(()=>document.body.dataset.testPhase==='window-checks-done',30000);
  // UI-only error injection after all real rendering/screenshot checks.
  $('canvas').dispatchEvent(new Event('webglcontextlost',{cancelable:true}));
  await until(()=>control('effect-status').textContent==='美颜不可用');
  check('render_failure_not_success',$('effect-status').textContent==='美颜不可用'&&!$('status').hidden&&v.paused);
  v.dispatchEvent(new Event('loadeddata'));
  check('error_survives_media_status_refresh',!$('status').hidden&&$('status').textContent.includes('显示中断'));
}catch(error){
  result.errors.push(String(error));result.statusText=$('status').textContent;
  result.video={width:v.videoWidth,height:v.videoHeight,time:v.currentTime,paused:v.paused,readyState:v.readyState,error:v.error?.message};
  result.engine=document.body.dataset.engine;result.faces=document.body.dataset.faces;
  try{result.image=(await pixels()).png;}catch{}
}
result.status=Object.values(result.checks).every(Boolean)&&!result.errors.length?'passed':'failed';
await fetch('test-result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(result)});
document.body.dataset.test=result.status;
