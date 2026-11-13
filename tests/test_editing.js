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
    fetch:async url=>({ok:true,json:async()=>({version:1,gamma:.8}),blob:async()=>({})})});
  vm.runInContext(fs.readFileSync('catalogue/web/app.js','utf8'),context);
  vm.runInContext('previous=1; objectURL="blob:original";',context);
  await el('edit').onclick();
  assert.equal(el('gamma').value,.8);
  document.fullscreenElement=el('stage');
  assert.equal(vm.runInContext('editing',context),true);
  el('reset-edit').onclick();
  assert.equal(el('gamma').value,1);
  const first=vm.runInContext('renderEdit(.5,1440,editSequence)',context);
  await new Promise(setImmediate);
  vm.runInContext('stopRenders()',context);
  pending.shift()(); await first;
  assert.notEqual(el('photo').src,'blob:0');
  await el('cancel-edit').onclick();
  assert.equal(el('photo').src,'blob:original');
  assert.equal(vm.runInContext('editing',context),false);
  assert.equal(vm.runInContext('savedRecipe.gamma',context),.8);
});
