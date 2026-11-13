'use strict';
const $ = id => document.getElementById(id);
let playing = true, busy = false, timer = null, previous = null, objectURL = null;
let seconds = 10, active = null, generation = 0;
let editing = false, savedRecipe = {version:1, gamma:1, pop:0}, editSequence = 0;
let previewTimer, highTimer, previewController, highController, sessionURL = null;
const history = [];
const photoZoom = createPhotoZoom($('stage'), $('photo'));
function updateControls() {
  $('back').disabled = history.length < 2;
  $('chronological-next').disabled = previous === null;
  $('chronological-previous').disabled = previous === null;
}
function schedule(delay = seconds * 1000) {
  clearTimeout(timer);
  if (playing && !editing) timer = setTimeout(next, delay);
}
function toggle() {
  playing = !playing;
  $('play').textContent = playing ? 'Pause' : 'Play';
  $('play').setAttribute('aria-label', playing ? 'Pause slideshow' : 'Play slideshow');
  if (playing) schedule(); else clearTimeout(timer);
}
async function next(mode = 'random') {
  if (editing) return;
  if (mode === 'back' && history.length < 2) return;
  if (['next', 'previous'].includes(mode) && previous === null) return;
  stopRenders();
  if (active) active.abort();
  const request = ++generation;
  const controller = new AbortController();
  active = controller;
  const deadline = setTimeout(() => controller.abort(), 35000);
  busy = true;
  clearTimeout(timer);
  $('next').textContent = 'Next Random →';
  $('hint').textContent = 'Loading photograph… Click Next Random to skip.';
  let success = false;
  try {
    let data;
    if (mode === 'back') {
      data = history[history.length - 2];
    } else {
      const url = mode === 'random'
        ? '/api/random' + (previous ? '?previous=' + previous : '')
        : '/api/adjacent?current=' + previous + '&direction=' + mode;
      const response = await fetch(url, {signal: controller.signal});
      data = await response.json();
      if (request !== generation) return;
      if (!response.ok) throw new Error(data.error);
      if (data.end) {
        $('hint').textContent = mode === 'next' ? 'This is the last available image chronologically.' : 'This is the first available image chronologically.';
        success = true;
        return;
      }
    }
    const imageResponse = await fetch(data.url, {signal: controller.signal});
    if (!imageResponse.ok) throw new Error('Photo could not be displayed. Trying another…');
    const nextURL = URL.createObjectURL(await imageResponse.blob());
    const preload = new Image();
    preload.src = nextURL;
    try {
      await Promise.race([preload.decode(), new Promise((resolve, reject) => {
        if (controller.signal.aborted) reject(new Error('Loading cancelled'));
        else controller.signal.addEventListener('abort', () => reject(new Error('Loading cancelled')), {once:true});
      })]);
    } catch (error) { URL.revokeObjectURL(nextURL); throw error; }
    if (request !== generation) { URL.revokeObjectURL(nextURL); return; }
    $('photo').src = nextURL;
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = nextURL;
    $('photo').alt = data.filename;
    $('photo').style.display = 'block';
    photoZoom.reset(preload.naturalWidth, preload.naturalHeight, document.fullscreenElement === $('stage'));
    $('placeholder').hidden = true;
    $('filename').textContent = data.filename;
    $('path').textContent = data.path;
    $('date').textContent = data.date ? data.date.replace(/\.\d+$/, '') : 'CAPTURE DATE UNKNOWN';
    $('camera').textContent = data.camera;
    $('collection').textContent = Number(data.count).toLocaleString() + ' photographs catalogued';
    $('hint').textContent = 'Scroll down/up to zoom · Space to pause · ←/→ chronological · B back · R random · F fullscreen';
    if (mode === 'back') history.pop();
    else history.push(data);
    previous = data.id;
    $('edit').disabled = false;
    updateControls();
    success = true;
  } catch (error) {
    if (request !== generation) return;
    const message = controller.signal.aborted ? 'Photo took too long. Trying another…' : (error.message || 'Unable to load photo; retrying shortly.');
    $('status').textContent = message;
    $('hint').textContent = message;
  } finally {
    clearTimeout(deadline);
    if (request !== generation) return;
    active = null;
    busy = false;
    $('next').textContent = 'Next Random →';
    schedule(success ? seconds * 1000 : 5000);
  }
}
async function fullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await $('stage').requestFullscreen();
  } catch (error) { $('hint').textContent = 'Fullscreen is unavailable in this browser.'; }
}
$('play').onclick = toggle;
$('next').onclick = () => next();
$('back').onclick = () => next('back');
$('chronological-next').onclick = () => next('next');
$('chronological-previous').onclick = () => next('previous');
$('fullscreen').onclick = fullscreen;
$('interval').onchange = () => {
  seconds = Math.max(1, Math.min(3600, Number($('interval').value) || 10));
  $('interval').value = seconds;
  try { localStorage.setItem('popeye-interval', String(seconds)); } catch (error) { /* Preferences are optional. */ }
  schedule();
};
document.addEventListener('keydown', event => {
  const focused = document.activeElement;
  if (focused && (['INPUT', 'TEXTAREA', 'SELECT'].includes(focused.tagName) || focused.isContentEditable)) return;
  if (event.code === 'ArrowRight') { event.preventDefault(); next('next'); }
  if (event.code === 'ArrowLeft') { event.preventDefault(); next('previous'); }
  if (focused && focused.tagName === 'BUTTON') return;
  if (event.code === 'Space') { event.preventDefault(); toggle(); }
  if (event.key.toLowerCase() === 'b') next('back');
  if (event.key.toLowerCase() === 'r') next();
  if (event.key.toLowerCase() === 'f') fullscreen();
});

function stopRenders() {
  ++editSequence;
  clearTimeout(previewTimer); clearTimeout(highTimer);
  if (previewController) previewController.abort();
  if (highController) highController.abort();
}
async function renderEdit(value, edge, sequence) {
  const controller = new AbortController();
  if (edge) previewController = controller; else highController = controller;
  let url;
  try {
    const response = await fetch(`/api/render/${previous}?gamma=${value.gamma}&pop=${value.pop}&edge=${edge}`, {signal:controller.signal});
    if (!response.ok) throw new Error('Render unavailable. Check pyvips installation or try again.');
    url = URL.createObjectURL(await response.blob());
    const image = new Image(); image.src = url;
    await image.decode();
    if (sequence !== editSequence || controller.signal.aborted) return;
    // Keep the existing CSS geometry and pan transform across resolution swaps.
    $('photo').src = url;
    if (objectURL && objectURL !== sessionURL) URL.revokeObjectURL(objectURL);
    objectURL = url; url = null;
    $('edit-status').textContent = edge ? 'Preview ready · refining…' : 'Full resolution ready';
  } catch (error) {
    if (sequence === editSequence && !controller.signal.aborted) $('edit-status').textContent = error.message;
  } finally { if (url) URL.revokeObjectURL(url); }
}
function currentRecipe() {
  return {version:1, gamma:Number($('gamma').value), pop:Number($('pop').value)};
}
function adjust() {
  stopRenders();
  const sequence = editSequence, value = currentRecipe();
  $('gamma-value').textContent = value.gamma.toFixed(2);
  $('pop-value').textContent = value.pop;
  $('edit-status').textContent = 'Rendering preview…';
  // Coalesce rapid input; high resolution starts only after preview completes.
  const preview = new Promise(resolve => {
    previewTimer = setTimeout(async () => { await renderEdit(value, 1440, sequence); resolve(); }, 60);
  });
  highTimer = setTimeout(async () => {
    await preview;
    if (sequence === editSequence) renderEdit(value, 0, sequence);
  }, 800);
}
function closeEditor() {
  if (sessionURL && sessionURL !== objectURL) URL.revokeObjectURL(sessionURL);
  sessionURL = null;
  editing = false; $('editor').hidden = true; $('edit').disabled = false;
  $('save-edit').disabled = false; $('cancel-edit').disabled = false;
  $('gamma').disabled = false; $('pop').disabled = false; $('reset-edit').disabled = false;
  schedule();
}
$('edit').onclick = async () => {
  if (busy || editing || previous === null) return;
  editing = true; clearTimeout(timer); $('edit').disabled = true;
  try {
    const response = await fetch('/api/edits/' + previous);
    if (!response.ok) throw new Error('Could not load saved edits');
    savedRecipe = await response.json();
    sessionURL = objectURL;
    $('gamma').value = savedRecipe.gamma;
    $('pop').value = savedRecipe.pop ?? 0;
    $('pop-value').textContent = savedRecipe.pop ?? 0;
    $('gamma-value').textContent = Number(savedRecipe.gamma).toFixed(2);
    $('edit-status').textContent = '';
    $('editor').hidden = false;
  } catch (error) { closeEditor(); $('hint').textContent = error.message; }
};
$('gamma').oninput = adjust;
$('pop').oninput = adjust;
$('reset-edit').onclick = () => { $('gamma').value = 1; $('pop').value = 0; adjust(); };
$('cancel-edit').onclick = async () => {
  stopRenders();
  $('save-edit').disabled = true; $('cancel-edit').disabled = true;
  $('gamma').disabled = true; $('pop').disabled = true; $('reset-edit').disabled = true;
  if (objectURL && objectURL !== sessionURL) URL.revokeObjectURL(objectURL);
  objectURL = sessionURL; $('photo').src = sessionURL;
  closeEditor();
};
$('save-edit').onclick = async () => {
  $('save-edit').disabled = true; $('cancel-edit').disabled = true;
  $('gamma').disabled = true; $('pop').disabled = true; $('reset-edit').disabled = true;
  try {
    const response = await fetch('/api/edits/' + previous, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(currentRecipe())});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save edits');
    savedRecipe = data;
    // A quick Save must still display the committed value before leaving editing.
    stopRenders();
    await renderEdit(data, 1440, editSequence);
    renderEdit(data, 0, editSequence);
    closeEditor();
  } catch (error) {
    $('edit-status').textContent = error.message;
    $('save-edit').disabled = false; $('cancel-edit').disabled = false;
    $('gamma').disabled = false; $('pop').disabled = false; $('reset-edit').disabled = false;
  }
};

updateControls();
(async () => {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    let config;
    try { config = await (await fetch('/api/config', {signal:controller.signal})).json(); }
    finally { clearTimeout(timeout); }
    seconds = Math.max(1, Math.min(3600, Number(localStorage.getItem('popeye-interval')) || config.interval));
    $('interval').value = seconds;
  } catch (error) { /* The default interval still works if preferences are unavailable. */ }
  next();
})();
