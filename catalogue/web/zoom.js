'use strict';
// Work in CSS pixels; devicePixelRatio converts the 8x screen-pixel limit.
function createPhotoZoom(stage, photo) {
  const step = Math.pow(2, 1 / 4);
  let width = 0, height = 0, scale = 1, fit = 1, maximum = 8;
  let x = 0, y = 0, frame = null, viewWidth = 0, viewHeight = 0;
  let drag = null;
  function canPan() {
    return width * scale > stage.clientWidth || height * scale > stage.clientHeight;
  }
  function endDrag() {
    if (!drag) return;
    const pointerId = drag.id;
    drag = null;
    if (stage.hasPointerCapture(pointerId)) stage.releasePointerCapture(pointerId);
    queueDraw();
  }
  function draw() {
    frame = null;
    photo.style.cursor = canPan() ? (drag ? 'grabbing' : 'grab') : 'default';
    photo.style.width = `${width}px`;
    photo.style.height = `${height}px`;
    photo.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
  }
  function queueDraw() {
    if (frame === null) frame = requestAnimationFrame(draw);
  }
  function constrain() {
    const scaledWidth = width * scale, scaledHeight = height * scale;
    x = scaledWidth <= stage.clientWidth ? (stage.clientWidth - scaledWidth) / 2
      : Math.max(stage.clientWidth - scaledWidth, Math.min(0, x));
    y = scaledHeight <= stage.clientHeight ? (stage.clientHeight - scaledHeight) / 2
      : Math.max(stage.clientHeight - scaledHeight, Math.min(0, y));
  }
  function reset(imageWidth = width, imageHeight = height, preserve = false) {
    const keepView = preserve && width > 0 && height > 0;
    endDrag();
    width = imageWidth;
    height = imageHeight;
    if (!width || !height || !stage.clientWidth || !stage.clientHeight) return;
    fit = Math.min(stage.clientWidth / width, stage.clientHeight / height);
    maximum = Math.max(fit, 8 / (window.devicePixelRatio || 1));
    scale = keepView ? Math.max(fit, Math.min(maximum, scale)) : fit;
    viewWidth = stage.clientWidth;
    viewHeight = stage.clientHeight;
    constrain();
    queueDraw();
  }
  // Disable native image dragging; pointer capture keeps a pan active outside the view.
  photo.draggable = false;
  stage.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target !== photo || !canPan() || drag) return;
    event.preventDefault();
    stage.setPointerCapture(event.pointerId);
    drag = {id: event.pointerId, x: event.clientX, y: event.clientY};
    queueDraw();
  });
  stage.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId) return;
    if (!(event.buttons & 1)) { endDrag(); return; }
    x += event.clientX - drag.x;
    y += event.clientY - drag.y;
    drag.x = event.clientX;
    drag.y = event.clientY;
    constrain();
    queueDraw();
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    stage.addEventListener(type, event => {
      if (drag && drag.id === event.pointerId) endDrag();
    });
  }
  stage.addEventListener('wheel', event => {
    if (!width || !height || event.ctrlKey || !event.deltaY) return;
    event.preventDefault();
    // Apply every delta, including high-resolution wheels and trackpads.
    // Up zooms in. Four standard notches (100px or three lines each) double it.
    const notches = -event.deltaY * (event.deltaMode === 1 ? 1 / 3
      : event.deltaMode === 2 ? 1 : 1 / 100);
    if (!Number.isFinite(notches)) return;
    const exponent = Math.max(-100, Math.min(100, notches));
    const nextScale = Math.max(fit, Math.min(maximum, scale * Math.pow(step, exponent)));
    if (nextScale === scale) return;
    const rect = stage.getBoundingClientRect();
    const cursorX = event.clientX - rect.left - stage.clientLeft;
    const cursorY = event.clientY - rect.top - stage.clientTop;
    const ratio = nextScale / scale;
    x = cursorX - (cursorX - x) * ratio;
    y = cursorY - (cursorY - y) * ratio;
    scale = nextScale;
    constrain();
    queueDraw();
  }, {passive: false});
  // Refit on window resizing and fullscreen changes, using the new viewport.
  new ResizeObserver(() => {
    if (stage.clientWidth !== viewWidth || stage.clientHeight !== viewHeight) reset();
  }).observe(stage);
  return {reset};
}
