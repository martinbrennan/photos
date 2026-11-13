// Run with: node tests/test_navigation.js
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
test('arrows navigate chronologically with button focus in both views, while fields retain editing keys',()=>{
  const elements=new Map(), calls=[], listeners={};
  const element=id=>{if(!elements.has(id)) elements.set(id,{tagName:'BUTTON'});return elements.get(id);};
  const document={getElementById:element,activeElement:{tagName:'BUTTON'},
    addEventListener(type,callback){listeners[type]=callback;}};
  const context=vm.createContext({document,calls,createPhotoZoom:()=>({reset(){}}),
    AbortController,setTimeout(){},clearTimeout(){},fetch:()=>new Promise(()=>{})});
  vm.runInContext(fs.readFileSync('catalogue/web/app.js','utf8'),context);
  vm.runInContext('next = mode => calls.push(mode)',context);
  for(const fullscreen of [false,true]) {
    document.fullscreenElement=fullscreen?element('stage'):null;
    for(const [code,expected] of [['ArrowRight','next'],['ArrowLeft','previous']]) {
      let prevented=false;
      listeners.keydown({code,key:code,preventDefault(){prevented=true;}});
      assert.equal(calls.pop(),expected);
      assert.equal(prevented,true);
    }
  }
  for(const focused of [{tagName:'INPUT'},{tagName:'TEXTAREA'},{tagName:'SELECT'},{tagName:'DIV',isContentEditable:true}]) {
    document.activeElement=focused;
    listeners.keydown({code:'ArrowRight',key:'ArrowRight',preventDefault(){assert.fail('Editing key intercepted');}});
    assert.equal(calls.length,0);
  }
});
