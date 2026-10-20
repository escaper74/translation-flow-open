// Opt-in integration driver for the fresh Edge test profile, never a user browser.
// Uses Node's built-in WebSocket; no production or test package installation.
import {writeFile} from 'node:fs/promises';
import {join} from 'node:path';
const [port,output,playerUrl]=process.argv.slice(2),base=`http://127.0.0.1:${Number(port)}`;
const result={checks:{}};
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(fn,timeout=80000){const end=Date.now()+timeout;while(Date.now()<end){const value=await fn();if(value)return value;await delay(100);}throw Error('Integration step timed out');}
function check(name,condition){result.checks[name]=Boolean(condition);if(!condition)throw Error(name);}
async function connect(url){
  const socket=new WebSocket(url),pending=new Map();let serial=0;
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
  socket.onmessage=event=>{const message=JSON.parse(event.data),request=pending.get(message.id);if(request){pending.delete(message.id);message.error?request.reject(Error(JSON.stringify(message.error))):request.resolve(message.result);}};
  return {close:()=>socket.close(),send(method,params={}){return new Promise((resolve,reject)=>{const id=++serial;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});}};
}
const clients=[];
let browser;
try{
  browser=await connect((await(await fetch(base+'/json/version')).json()).webSocketDebuggerUrl);clients.push(browser);
  const targets=()=>fetch(base+'/json/list').then(r=>r.json());
  const target=await until(async()=>(await targets()).find(t=>t.type==='page'&&t.url===playerUrl));
  const player=await connect(target.webSocketDebuggerUrl);clients.push(player);
  const evaluate=async(client,expression,userGesture=false)=>{
    const value=await client.send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true,userGesture});
    if(value.exceptionDetails)throw Error(JSON.stringify(value.exceptionDetails));return value.result.value;
  };
  const phase=()=>evaluate(player,'document.body?.dataset.testPhase');
  for(const step of ['open-tools','reopen-tools']){
    result.step=step;
    await until(async()=>await phase()===step||await evaluate(player,'document.body?.dataset.test')==='failed');
    if(await evaluate(player,'document.body.dataset.test')==='failed')throw Error('Media fixture failed before '+step);
    await evaluate(player,"document.getElementById('beauty-tools').click()",true);
    await until(async()=>await phase()!==step||await evaluate(player,'document.body?.dataset.test')==='failed');
  }
  await until(async()=>await phase()==='window-checks'||await evaluate(player,'document.body?.dataset.test')==='failed');
  if(await evaluate(player,'document.body.dataset.test')==='failed')throw Error('Media fixture failed');
  const toolTarget=await until(async()=>(await targets()).find(t=>t.type==='page'&&t.url===playerUrl+'toolbar.html'));
  const toolbar=await connect(toolTarget.webSocketDebuggerUrl);clients.push(toolbar);
  const playerWindow=await browser.send('Browser.getWindowForTarget',{targetId:target.id});
  const toolWindow=await browser.send('Browser.getWindowForTarget',{targetId:toolTarget.id});
  check('different_browser_windows',playerWindow.windowId!==toolWindow.windowId);
  const measure=client=>evaluate(client,`({width:innerWidth,height:innerHeight,font:getComputedStyle(document.querySelector('button')).fontSize,stage:document.getElementById('stage')?.getBoundingClientRect().width,canvas:document.getElementById('canvas')?.getBoundingClientRect().width})`);
  const playerBefore=await measure(player),toolBefore=await measure(toolbar);
  check('compact_toolbar',toolBefore.width<=340&&toolBefore.height<=520);
  check('toolbar_controls_fit',await evaluate(toolbar,'document.documentElement.scrollHeight<=innerHeight'));
  const screenshot=async(client,name)=>{
    const {data}=await client.send('Page.captureScreenshot',{format:'png'});
    await writeFile(join(output,name),Buffer.from(data,'base64'));
  };
  await screenshot(player,'player-wide.png');await screenshot(toolbar,'toolbar.png');
  await browser.send('Browser.setWindowBounds',{windowId:playerWindow.windowId,bounds:{width:960,height:680}});
  await until(async()=>(await measure(player)).width<playerBefore.width);
  const playerSmall=await measure(player),toolAfter=await measure(toolbar);
  check('video_window_resizes_independently',playerSmall.stage<playerBefore.stage&&JSON.stringify(toolBefore)===JSON.stringify(toolAfter));
  check('player_font_not_scaled',playerSmall.font===playerBefore.font);
  await screenshot(player,'player-small.png');
  await evaluate(player,"document.getElementById('zoom-in').click()");
  const playerZoom=await measure(player);
  check('zoom_only_changes_video',Math.abs(playerZoom.canvas/playerSmall.canvas-1.1)<.01&&playerZoom.stage===playerSmall.stage&&playerZoom.font===playerSmall.font&&JSON.stringify(await measure(toolbar))===JSON.stringify(toolAfter));
  await screenshot(player,'player-zoom.png');
  await evaluate(player,"document.getElementById('zoom-reset').click()");
  await evaluate(player,"document.getElementById('fullscreen').click()",true);
  await until(()=>evaluate(player,'Boolean(document.fullscreenElement)'));
  check('fullscreen_keeps_transport',await evaluate(player,"Boolean(document.fullscreenElement.contains(document.getElementById('play')))"));
  await screenshot(player,'player-fullscreen.png');
  await evaluate(player,"document.getElementById('fullscreen').click()",true);
  await until(async()=>!(await evaluate(player,'Boolean(document.fullscreenElement)')));
  await browser.send('Browser.setWindowBounds',{windowId:toolWindow.windowId,bounds:{left:120,top:120}});
  const moved=await browser.send('Browser.getWindowBounds',{windowId:toolWindow.windowId});
  check('toolbar_has_independent_position',moved.bounds.left===120&&moved.bounds.top===120);
  result.measurements={playerBefore,playerSmall,playerZoom,toolBefore,toolAfter};
  await evaluate(player,"document.body.dataset.testPhase='window-checks-done'");
  await until(()=>evaluate(player,'document.body.dataset.test'));
  await browser.send('Target.closeTarget',{targetId:target.id});
  await until(async()=>!(await targets()).some(t=>t.id===toolTarget.id),5000);
  check('closing_player_closes_toolbar',true);
  result.status='passed';
}catch(error){
  result.status='failed';result.error=String(error);process.exitCode=1;result.pages=[];
  for(const page of await(await fetch(base+'/json/list')).json()){
    if(page.type!=='page'||!page.url.startsWith(playerUrl))continue;
    const client=await connect(page.webSocketDebuggerUrl);clients.push(client);
    result.pages.push({url:page.url,state:await client.send('Runtime.evaluate',{expression:'({text:document.body?.innerText,ready:document.readyState,opener:Boolean(window.opener),phase:document.body?.dataset.testPhase})',returnByValue:true})});
    const {data}=await client.send('Page.captureScreenshot',{format:'png'});await writeFile(join(output,`failure-${result.pages.length}.png`),Buffer.from(data,'base64'));
  }
}
finally{
  await writeFile(join(output,'window-result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
  // Close only this test's isolated browser, including Edge launcher hand-offs.
  if(browser)await Promise.race([browser.send('Browser.close').catch(()=>{}),delay(3000)]);
  for(const client of clients)client.close();
}
