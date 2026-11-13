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
    fetch:async url=>({ok:true,json:async()=>({version:1,gamma:.8,pop:40}),blob:async()=>({})})});
  vm.runInContext(fs.readFileSync('catalogue/web/app.js','utf8'),context);
  vm.runInContext('previous=1; objectURL="blob:original";',context);
  await el('edit').onclick();
  assert.equal(el('gamma').value,.8);
  assert.equal(el('pop').value,40);
  document.fullscreenElement=el('stage');
  assert.equal(vm.runInContext('editing',context),true);
  el('reset-edit').onclick();
  assert.equal(el('gamma').value,1);
  assert.equal(el('pop').value,0);
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
test('Save commits Gamma and Pop together and refines the committed preview',async()=>{
  const elements=new Map(), requests=[];
  const el=id=>{if(!elements.has(id))elements.set(id,{value:1,style:{},setAttribute(){}});return elements.get(id);};
  const context=vm.createContext({document:{getElementById:el,addEventListener(){}},
    createPhotoZoom:()=>({reset(){throw Error('render must preserve zoom');}}),AbortController,
    setTimeout(){return 1;},clearTimeout(){},localStorage:{},
    URL:{createObjectURL:()=>`blob:${requests.length}`,revokeObjectURL(){}},
    Image:class{async decode(){}},
    fetch:async(url,options={})=>{requests.push({url,options});return {ok:true,
      json:async()=>url.startsWith('/api/edits')?{version:1,gamma:.85,pop:45}:{interval:10},
      blob:async()=>({})};}});
  vm.runInContext(fs.readFileSync('catalogue/web/app.js','utf8'),context);
  await new Promise(setImmediate);
  vm.runInContext('previous=1;busy=false;objectURL="blob:original";',context);
  await el('edit').onclick();
  el('gamma').value=.85;el('pop').value=45;
  await el('save-edit').onclick();
  await new Promise(setImmediate);
  const save=requests.find(r=>r.options.method==='POST');
  assert.deepEqual(JSON.parse(save.options.body),{version:1,gamma:.85,pop:45});
  assert.ok(requests.some(r=>r.url==='/api/render/1?gamma=0.85&pop=45&edge=1440'));
  assert.ok(requests.some(r=>r.url==='/api/render/1?gamma=0.85&pop=45&edge=0'));
  assert.equal(vm.runInContext('editing',context),false);
});
