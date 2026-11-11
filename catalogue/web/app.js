'use strict';
const $ = id => document.getElementById(id);
let playing = true, busy = false, timer = null, previous = null, objectURL = null;
let seconds = 10, active = null, generation = 0;
const history = [];
const photoZoom = createPhotoZoom($('stage'), $('photo'));
function updateControls() {
  $('back').disabled = history.length < 2;
  $('chronological-next').disabled = previous === null;
  $('chronological-previous').disabled = previous === null;
}
function schedule(delay = seconds * 1000) {
  clearTimeout(timer);
  if (playing) timer = setTimeout(next, delay);
}
function toggle() {
  playing = !playing;
  $('play').textContent = playing ? 'Pause' : 'Play';
  $('play').setAttribute('aria-label', playing ? 'Pause slideshow' : 'Play slideshow');
  if (playing) schedule(); else clearTimeout(timer);
}
async function next(mode = 'random') {
  if (mode === 'back' && history.length < 2) return;
  if (['next', 'previous'].includes(mode) && previous === null) return;
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
    photoZoom.reset(preload.naturalWidth, preload.naturalHeight);
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
  if (['INPUT', 'BUTTON', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) return;
  if (event.code === 'Space') { event.preventDefault(); toggle(); }
  if (event.code === 'ArrowRight') { event.preventDefault(); next('next'); }
  if (event.code === 'ArrowLeft') { event.preventDefault(); next('previous'); }
  if (event.key.toLowerCase() === 'b') next('back');
  if (event.key.toLowerCase() === 'r') next();
  if (event.key.toLowerCase() === 'f') fullscreen();
});
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
