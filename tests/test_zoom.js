// Run with: node --test tests/test_zoom.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function viewer(pixelRatio = 1) {
  const stage = {clientWidth:1000, clientHeight:800, clientLeft:1, clientTop:1,
    getBoundingClientRect:()=>({left:10,top:20}),
    addEventListener(type, handler, options) {this[type]=handler;if(type==='wheel') assert.equal(options.passive,false);},
    setPointerCapture(id){this.capture=id;},hasPointerCapture(id){return this.capture===id;},
    releasePointerCapture(){this.capture=null;}};
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
  assert.match(photo.style.transform,/scale\(4\)/);
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

function assertTransform(photo, x, y, scale) {
  const actual = photo.style.transform.match(/-?[\d.]+/g).map(Number);
  [x,y,scale].forEach((value,index)=>assert.ok(Math.abs(actual[index]-value)<1e-8,
    `Transform ${actual} differs from ${[x,y,scale]}`));
}
function pointer(view, type, x, y, extras={}) {
  view.stage[type]({pointerId:1,button:0,buttons:1,target:view.photo,
    clientX:x,clientY:y,preventDefault(){},...extras});
  view.flush();
}
test('left drag pans, clamps all edges, and reverses immediately at an edge',()=>{
  const v=viewer();
  v.wheel(-400);
  assert.equal(v.photo.draggable,false);
  pointer(v,'pointerdown',400,300);
  assert.equal(v.photo.style.cursor,'grabbing');
  pointer(v,'pointermove',500,400);
  assertTransform(v.photo,-300,-200,1);
  pointer(v,'pointermove',5000,5000);
  assertTransform(v.photo,0,0,1);
  pointer(v,'pointermove',4990,4990);
  assertTransform(v.photo,-10,-10,1);
  pointer(v,'pointermove',-5000,-5000);
  assertTransform(v.photo,-1000,-800,1);
  pointer(v,'pointerup',-5000,-5000);
  assert.equal(v.stage.capture,null);
  assert.equal(v.photo.style.cursor,'grab');
});
test('fit image stays centred, controls and right clicks do not pan',()=>{
  const v=viewer();
  pointer(v,'pointerdown',400,300);
  pointer(v,'pointermove',500,400);
  assertTransform(v.photo,0,0,0.5);
  v.wheel(-400);
  const before=v.photo.style.transform;
  pointer(v,'pointerdown',400,300,{button:2});
  pointer(v,'pointermove',500,400);
  assert.equal(v.photo.style.transform,before);
  pointer(v,'pointerdown',400,300,{target:{}});
  pointer(v,'pointermove',500,400);
  assert.equal(v.photo.style.transform,before);
});
test('cancel and image changes end dragging; letterboxed axis remains centred',()=>{
  const v=viewer();
  v.zoom.reset(2000,400);
  v.wheel(-400);
  pointer(v,'pointerdown',400,300);
  pointer(v,'pointermove',5000,5000);
  assertTransform(v.photo,0,200,1);
  pointer(v,'pointercancel',5000,5000);
  assert.equal(v.stage.capture,null);
  pointer(v,'pointerdown',400,300);
  v.zoom.reset(2000,1600);
  assert.equal(v.stage.capture,null);
  pointer(v,'pointermove',500,400);
  assertTransform(v.photo,0,0,0.5);
});

test('preserved image changes keep zoom and pan, clamping to new image edges',()=>{
  const v=viewer();
  v.wheel(-400);
  pointer(v,'pointerdown',400,300);
  pointer(v,'pointermove',500,400);
  pointer(v,'pointerup',500,400);
  v.zoom.reset(2000,1600,true);
  assertTransform(v.photo,-300,-200,1);
  v.zoom.reset(1200,900,true);
  assertTransform(v.photo,-200,-100,1);
  v.zoom.reset(200,100,true);
  assertTransform(v.photo,0,150,5);
  v.zoom.reset(2000,1600);
  assertTransform(v.photo,0,0,.5);
});
