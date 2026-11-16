const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
test('editor preserves fullscreen session, resets, cancels and ignores obsolete decoded renders',async()=>{
  const elements=new Map(), pending=[], revoked=[];
  const el=id=>{if(!elements.has(id)) elements.set(id,{value:1,style:{},setAttribute(){}});return elements.get(id);};
  const document={getElementById:el,addEventListener(){},fullscreenElement:null};
  const context=vm.createContext({document,createPhotoZoom:()=>({reset(){}}),AbortController,
    setTimeout(){return 1;},clearTimeout(){},localStorage:{},
    URL:{createObjectURL:()=>`blob:${pending.length}`,revokeObjectURL:u=>revoked.push(u)},
    Image:class{decode(){return new Promise(resolve=>pending.push(resolve));}},
    fetch:async url=>({ok:true,json:async()=>({version:1,gamma:.8,pop:40,drama:{strength:60,saturation:-20}}),blob:async()=>({})})});
  vm.runInContext(fs.readFileSync('catalogue/web/app.js','utf8'),context);
  vm.runInContext('previous=1; objectURL="blob:original";',context);
  await el('edit').onclick();
  assert.equal(el('gamma').value,.8);
  assert.equal(el('pop').value,40);
  assert.equal(el('drama').value,60);
  assert.equal(el('drama-saturation').value,-20);
  document.fullscreenElement=el('stage');
  assert.equal(vm.runInContext('editing',context),true);
  el('reset-edit').onclick();
  assert.equal(el('gamma').value,1);
  assert.equal(el('pop').value,0);
  assert.equal(el('drama').value,0);
  assert.equal(el('drama-saturation').value,0);
  const first=vm.runInContext('renderEdit({gamma:.5,pop:70},1440,editSequence)',context);
  await new Promise(setImmediate);
  vm.runInContext('stopRenders()',context);
  pending.shift()(); await first;
  assert.notEqual(el('photo').src,'blob:0');
  await el('cancel-edit').onclick();
  assert.equal(el('photo').src,'blob:original');
  assert.equal(vm.runInContext('editing',context),false);
  assert.equal(vm.runInContext('savedRecipe.gamma',context),.8);
});
test('Save commits Gamma, Pop and Drama together and refines the committed preview',async()=>{
  const elements=new Map(), requests=[];
  const el=id=>{if(!elements.has(id))elements.set(id,{value:1,style:{},setAttribute(){}});return elements.get(id);};
  const context=vm.createContext({document:{getElementById:el,addEventListener(){}},
    createPhotoZoom:()=>({reset(){throw Error('render must preserve zoom');}}),AbortController,
    setTimeout(){return 1;},clearTimeout(){},localStorage:{},
    URL:{createObjectURL:()=>`blob:${requests.length}`,revokeObjectURL(){}},
    Image:class{async decode(){}},
    fetch:async(url,options={})=>{requests.push({url,options});return {ok:true,
      json:async()=>url.startsWith('/api/edits')?{version:1,gamma:.85,pop:45,drama:{strength:50,saturation:-25}}:{interval:10},
      blob:async()=>({})};}});
  vm.runInContext(fs.readFileSync('catalogue/web/app.js','utf8'),context);
  await new Promise(setImmediate);
  vm.runInContext('previous=1;busy=false;objectURL="blob:original";',context);
  await el('edit').onclick();
  el('gamma').value=.85;el('pop').value=45;el('drama').value=50;el('drama-saturation').value=-25;
  await el('save-edit').onclick();
  await new Promise(setImmediate);
  const save=requests.find(r=>r.options.method==='POST');
  assert.deepEqual(JSON.parse(save.options.body),{version:1,gamma:.85,pop:45,drama:{strength:50,saturation:-25}});
  assert.ok(requests.some(r=>r.url==='/api/render/1?gamma=0.85&pop=45&drama=50&drama_saturation=-25&edge=1440'));
  assert.ok(requests.some(r=>r.url==='/api/render/1?gamma=0.85&pop=45&drama=50&drama_saturation=-25&edge=0'));
  assert.equal(vm.runInContext('editing',context),false);
});
test('holding Space shows original and release restores the latest edit, including during rendering',async()=>{
  const elements=new Map(), listeners={}, requests=[];
  const el=id=>{if(!elements.has(id))elements.set(id,{value:1,style:{},setAttribute(){}});return elements.get(id);};
  const document={getElementById:el,addEventListener(type,fn){listeners[type]=fn;},activeElement:{tagName:'BUTTON'}};
  let serial=0;
  const context=vm.createContext({document,createPhotoZoom:()=>({reset(){}}),AbortController,
    setTimeout(){},clearTimeout(){},fetch:async url=>{
      requests.push(url);
      if(url==='/api/config')return new Promise(()=>{});
      return {ok:true,blob:async()=>({})};
    },URL:{createObjectURL:()=>`blob:${++serial}`,revokeObjectURL(){}},Image:class{async decode(){}}});
  vm.runInContext(fs.readFileSync('catalogue/web/app.js','utf8'),context);
  vm.runInContext('previous=1; objectURL="blob:edited"; editing=true;',context);
  const event={code:'Space',key:' ',preventDefault(){}};
  listeners.keydown(event);
  await new Promise(setImmediate);
  assert.equal(el('photo').src,'blob:1');
  assert.ok(requests.includes('/api/render/1?gamma=1&pop=0&drama=0&drama_saturation=0&edge=2560'));
  await vm.runInContext('renderEdit({gamma:.7,pop:20},1440,editSequence)',context);
  assert.equal(el('photo').src,'blob:1');
  listeners.keyup(event);
  assert.equal(el('photo').src,'blob:2');
  assert.equal(vm.runInContext('playing',context),true);
  listeners.keydown(event);
  assert.equal(el('photo').src,'blob:1');
  listeners.keyup(event);
  document.activeElement={tagName:'INPUT'};
  listeners.keydown({...event,preventDefault(){assert.fail('Input Space intercepted');}});
  assert.equal(el('photo').src,'blob:2');
});
test('Copy keeps an independent recipe across photos and Paste previews every adjustment',async()=>{
  const elements=new Map();
  const el=id=>{if(!elements.has(id))elements.set(id,{value:1,style:{},disabled:false,setAttribute(){}});return elements.get(id);};
  const context=vm.createContext({document:{getElementById:el,addEventListener(){}},
    createPhotoZoom:()=>({reset(){}}),AbortController,setTimeout(){},clearTimeout(){},
    fetch:()=>new Promise(()=>{}),URL:{revokeObjectURL(){}}});
  vm.runInContext(fs.readFileSync('catalogue/web/app.js','utf8'),context);
  vm.runInContext('editing=true;',context);
  el('gamma').value=.75;el('pop').value=35;el('drama').value=60;el('drama-saturation').value=-25;
  el('copy-edit').onclick();
  assert.equal(el('paste-edit').disabled,false);
  el('reset-edit').onclick();
  vm.runInContext('closeEditor(); previous=2; editing=true;',context);
  el('paste-edit').onclick();
  assert.equal(el('gamma').value,.75);
  assert.equal(el('pop').value,35);
  assert.equal(el('drama').value,60);
  assert.equal(el('drama-saturation').value,-25);
  assert.equal(el('gamma-value').textContent,'0.75');
  assert.equal(el('edit-status').textContent,'Rendering preview…');
  el('save-edit').disabled=true;
  el('gamma').value=1;
  el('paste-edit').onclick();
  assert.equal(el('gamma').value,1);
});
test('failed previews do not launch full resolution refinement',async()=>{
  const elements=new Map(), timers=[], requests=[];
  const el=id=>{if(!elements.has(id))elements.set(id,{value:1,style:{},setAttribute(){}});return elements.get(id);};
  const context=vm.createContext({document:{getElementById:el,addEventListener(){}},
    createPhotoZoom:()=>({reset(){}}),AbortController,
    setTimeout(fn,delay){timers.push({fn,delay});return timers.length;},clearTimeout(){},
    fetch:async url=>{if(url==='/api/config')return new Promise(()=>{});requests.push(url);return {ok:false};},
    URL:{revokeObjectURL(){}}});
  vm.runInContext(fs.readFileSync('catalogue/web/app.js','utf8'),context);
  vm.runInContext('previous=1; editing=true; adjust();',context);
  await timers.find(t=>t.delay===60).fn();
  await timers.find(t=>t.delay===800).fn();
  assert.equal(requests.length,1);
  assert.match(requests[0],/edge=1440$/);
  assert.equal(el('edit-status').textContent,'Could not render these settings. Try adjusting again.');
});
