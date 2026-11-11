// Run with: node --test tests/test_zoom.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function viewer(pixelRatio = 1) {
  const stage = {clientWidth:1000, clientHeight:800, clientLeft:1, clientTop:1,
    getBoundingClientRect:()=>({left:10,top:20}),
    addEventListener(type, handler, options) {this.wheel=handler; assert.equal(options.passive,false);}};
  const photo = {style:{}};
  let resize, pending = [];
  const context = vm.createContext({stage, photo, window:{devicePixelRatio:pixelRatio},
    requestAnimationFrame(callback){pending.push(callback);return pending.length;},
    ResizeObserver:class {constructor(handler){resize=handler;} observe(){}}});
  vm.runInContext(fs.readFileSync('catalogue/web/zoom.js','utf8'),context);
  const zoom = vm.runInContext('createPhotoZoom(stage, photo)',context);
  const flush = () => {const callbacks = pending; pending = []; callbacks.forEach(callback=>callback());};
  zoom.reset(2000,1600);
  flush();
  function wheel(deltaY, deltaMode=0, render=true) {
    let prevented=false;
    stage.wheel({deltaY,deltaMode,clientX:411,clientY:321,preventDefault(){prevented=true;}});
    assert.equal(prevented,true);
    if (render) flush();
  }
  return {stage,photo,flush,pendingFrames:()=>pending.length,zoom:{reset(...args){zoom.reset(...args);flush();}},wheel,resize:()=>{resize();flush();}};
}
test('four notches double scale around the cursor; reverse restores fit',()=>{
  const {photo,wheel}=viewer();
  assert.match(photo.style.transform,/scale\(0.5\)/);
  for(let i=0;i<4;i++) wheel(-100);
  assert.ok(Math.abs(2000 * Number(photo.style.transform.match(/scale\(([^)]+)\)/)[1])-2000)<1e-8);
  const offsets = photo.style.transform.match(/-?[\d.]+/g).map(Number);
  assert.ok(Math.abs(offsets[0]+400)<1e-8);
  assert.ok(Math.abs(offsets[1]+300)<1e-8);
  for(let i=0;i<8;i++) wheel(100);
  assert.match(photo.style.transform,/scale\(0.5\)/);
  assert.equal(photo.style.transform,'translate(0px, 0px) scale(0.5)');
});
test('maximum uses physical screen pixels; resize and new image reset zoom',()=>{
  const {stage,photo,zoom,wheel,resize}=viewer(2);
  wheel(-10000);
  assert.match(photo.style.transform,/scale\(1\)/);
  stage.clientWidth=1600; stage.clientHeight=1200; resize();
  assert.match(photo.style.transform,/scale\(0.75\)/);
  zoom.reset(100,100);
  wheel(10000);
  assert.equal(photo.style.width,'100px'); // Fit wins when already above the pixel cap.
});
test('small wheel deltas respond immediately and line wheels reverse cleanly',()=>{
  const {photo,wheel,resize}=viewer();
  wheel(-25);
  assert.ok(Number(photo.style.transform.match(/scale\(([^)]+)\)/)[1]) > .5);
  wheel(-25);wheel(-25);wheel(-25);
  assert.ok(Math.abs(2000 * Number(photo.style.transform.match(/scale\(([^)]+)\)/)[1])-1000*Math.pow(2,.25))<1e-8);
  resize(); // Same-size observer notifications must not reset zoom.
  assert.ok(Number(photo.style.transform.match(/scale\(([^)]+)\)/)[1]) > .5);
  wheel(3,1);
  assert.match(photo.style.transform,/scale\(0.5\)/);
  for(let i=0;i<2000;i++) wheel(-1);
  for(let i=0;i<2000;i++) wheel(1);
  assert.match(photo.style.transform,/scale\(0.5\)/);
});

test('wheel bursts retain all movement but schedule only one draw',()=>{
  const {photo,wheel,flush,pendingFrames}=viewer();
  for(let i=0;i<100;i++) wheel(-4,0,false);
  assert.equal(pendingFrames(),1);
  assert.match(photo.style.transform,/scale\(0.5\)/);
  flush();
  assert.ok(Math.abs(Number(photo.style.transform.match(/scale\(([^)]+)\)/)[1])-1)<1e-10);
  assert.equal(pendingFrames(),0);
});
