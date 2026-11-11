'use strict';
const $ = id => document.getElementById(id);
let playing = true, busy = false, timer = null, previous = null, objectURL = null;
let seconds = 10, active = null, generation = 0;
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
async function next() {
  if (active) active.abort();
  const request = ++generation;
  const controller = new AbortController();
  active = controller;
  const deadline = setTimeout(() => controller.abort(), 35000);
  busy = true;
  clearTimeout(timer);
  $('next').textContent = 'Skip loading photo →';
  $('hint').textContent = 'Loading photograph… Click Next to skip.';
  let success = false;
  try {
    const response = await fetch('/api/random' + (previous ? '?previous=' + previous : ''), {signal: controller.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
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
    $('placeholder').hidden = true;
    $('filename').textContent = data.filename;
    $('path').textContent = data.path;
    $('date').textContent = data.date ? data.date.replace(/\.\d+$/, '') : 'CAPTURE DATE UNKNOWN';
    $('camera').textContent = data.camera;
    $('collection').textContent = Number(data.count).toLocaleString() + ' photographs catalogued';
    $('hint').textContent = 'Space to pause · Right arrow for next · F for fullscreen';
    previous = data.id;
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
    $('next').textContent = 'Next photo →';
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
$('next').onclick = next;
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
  if (event.code === 'ArrowRight') { event.preventDefault(); next(); }
  if (event.key.toLowerCase() === 'f') fullscreen();
});
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
