const $=id=>document.getElementById(id),owner=window.opener;
const keys=['smooth','white','slim','eyes'];
// Some Edge app windows ignore window.open's initial size. This is still a
// script-created popup; request a compact content area without scaling its UI.
if(owner)window.resizeTo(300+window.outerWidth-window.innerWidth,475+window.outerHeight-window.innerHeight);
function send(data){if(owner&&!owner.closed)owner.postMessage(data,location.origin);}
function disconnected(){
  $('beauty-controls').disabled=true;$('effect-status').textContent='播放窗已关闭';
  $('status').hidden=false;$('status').textContent='请从播放窗重新打开美颜。';
}
window.addEventListener('message',event=>{
  if(event.origin!==location.origin||event.source!==owner||event.data?.type!=='beauty-state')return;
  const {settings,status,error,ready}=event.data;
  $('beauty-controls').disabled=false;$('enabled').checked=settings.enabled;
  for(const key of keys){$(key).value=settings[key];$(key+'-value').value=settings[key];}
  $('effect-status').textContent=status;$('effect-status').dataset.ready=String(ready);
  $('status').textContent=error;$('status').hidden=!error;$('status').classList.toggle('error',Boolean(error));
});
for(const key of keys)$(key).oninput=()=>{
  $(key+'-value').value=$(key).value;send({type:'beauty-change',key,value:Number($(key).value)});
};
$('enabled').onchange=()=>send({type:'beauty-change',key:'enabled',value:$('enabled').checked});
$('natural').onclick=()=>send({type:'beauty-preset',preset:'natural'});
$('reset').onclick=()=>send({type:'beauty-preset',preset:'reset'});
$('close-tools').onclick=()=>window.close();
window.addEventListener('pagehide',()=>send({type:'beauty-closed'}));
const ownerWatch=setInterval(()=>{if(!owner||owner.closed){clearInterval(ownerWatch);disconnected();if(owner)window.close();}},500);
if(owner&&!owner.closed)send({type:'beauty-ready'});else disconnected();
