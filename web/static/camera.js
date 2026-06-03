/* Camera card: live snapshot refresh, cover-state badge, and ROI calibration.

   We poll /api/camera/status independently from the chart's own /history poll:
   fewer races, simpler retry logic. */

(function () {
  'use strict';

  // i18n: window.I18N is injected by index.html for the current lang.
  // Plain string templates with {placeholders} so French translations stay
  // grammatical (FR adjectives go after the noun, English before — only
  // sentence-level translation handles that).
  var T = function (k, p) {
    var s = (window.I18N && window.I18N[k]) || k;
    if (p) { for (var x in p) s = s.split('{' + x + '}').join(p[x]); }
    return s;
  };

  // -- DOM --
  var card    = document.getElementById('cam-card');
  if (!card) return;             // server didn't render the card; subsystem is off
  var img     = document.getElementById('cam-img');
  var empty   = document.getElementById('cam-empty');
  var meta    = document.getElementById('cam-meta');
  var coverEl = document.getElementById('cam-cover');
  var canvas  = document.getElementById('cam-roi-canvas');
  var btnSettings = document.getElementById('cam-settings-btn');
  var settings    = document.getElementById('cam-settings');
  var btnRoi      = document.getElementById('cam-roi-btn');
  var btnRoiClearSaved = document.getElementById('cam-roi-clear-saved');
  var roiActs     = document.getElementById('cam-roi-actions');
  var btnCancel   = document.getElementById('cam-roi-cancel');
  var btnSave     = document.getElementById('cam-roi-save');
  var btnCoverOn  = document.getElementById('cam-cover-on');
  var btnCoverOff = document.getElementById('cam-cover-off');
  var btnCoverRst = document.getElementById('cam-cover-reset');
  var coverHelp   = document.getElementById('cam-cover-help');
  var recapLink = document.getElementById('cam-recap-link');
  var tlLink    = document.getElementById('cam-tl-link');

  function isoDate() {
    var d = new Date();
    function pad(n) { return ('0' + n).slice(-2); }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  recapLink.href = '/recap?date=' + isoDate();
  tlLink.href    = '/timelapse?date=' + isoDate();

  function fmtAge(s) {
    if (s == null) return '';
    if (s < 60) return Math.round(s) + ' s';
    if (s < 3600) return Math.round(s / 60) + ' min';
    return Math.round(s / 3600) + ' h';
  }

  // -- snapshot refresh --
  // Use last_frame_at as the cache-bust so we only re-download when there's
  // actually a new frame (saves bandwidth on iPhone polling).
  var lastShownAt = 0;
  function updateImage(snap) {
    if (!snap || !snap.frame_at) {
      img.hidden = true; empty.hidden = false; return;
    }
    if (snap.frame_at !== lastShownAt) {
      img.src = '/camera.jpg?ts=' + Math.floor(snap.frame_at);
      lastShownAt = snap.frame_at;
    }
    img.hidden = false; empty.hidden = true;
  }

  function updateMeta(snap) {
    if (!snap || !snap.enabled) { meta.textContent = ''; return; }
    var bits = [];
    if (snap.age_s != null) bits.push('🕒 ' + fmtAge(snap.age_s));
    if (snap.error) bits.push('⚠️ ' + snap.error.split('\n')[0].slice(0, 50));
    meta.textContent = bits.join(' · ');
  }

  function updateCover(snap) {
    var c = snap && snap.cover;
    coverEl.classList.remove('cover-on', 'cover-off', 'cover-unknown');
    var forced = !!(c && c.forced);
    if (!c || !c.state) {
      coverEl.textContent = T('cam.cover_label');
      coverEl.classList.add('cover-unknown'); return;
    }
    var pct = Math.round((c.confidence || 0) * 100);
    if (c.state === 'on') {
      coverEl.textContent = forced ? T('cam.cover_forced_on') : T('cam.cover_on', {pct: pct});
      coverEl.classList.add('cover-on');
    } else if (c.state === 'off') {
      coverEl.textContent = forced ? T('cam.cover_forced_off') : T('cam.cover_off', {pct: pct});
      coverEl.classList.add('cover-off');
    } else {
      coverEl.textContent = T('cam.cover_unknown');
      coverEl.classList.add('cover-unknown');
    }
    // calibration helper: show baseline status
    if (snap && snap.baselines && coverHelp) {
      var b = snap.baselines;
      var pieces = [];
      pieces.push(
        (b.on && typeof b.on.luma === 'number')
          ? T('cam.learn_baseline_on',  {luma: Math.round(b.on.luma),  std: Math.round(b.on.std)})
          : T('cam.learn_baseline_missing_on')
      );
      pieces.push(
        (b.off && typeof b.off.luma === 'number')
          ? T('cam.learn_baseline_off', {luma: Math.round(b.off.luma), std: Math.round(b.off.std)})
          : T('cam.learn_baseline_missing_off')
      );
      coverHelp.textContent = pieces.join(' · ');
    }
    // segmented selector: highlight current force-state
    var current = snap.forced_state || 'auto';
    document.querySelectorAll('.cam-seg').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-force') === current);
    });
  }

  // -- /api/camera/status loop --
  async function pollStatus() {
    try {
      var r = await fetch('/api/camera/status');
      if (!r.ok) return;
      var snap = await r.json();
      if (!snap.enabled) return;
      updateImage(snap);
      updateMeta(snap);
      updateCover(snap);
    } catch (e) { /* network blip; try next tick */ }
  }

  // -- ROI calibration ---------------------------------------------------
  // Draw a rectangle on top of the live image; POST {x,y,w,h} mapped to the
  // FRAME's native pixels (the camera returns ~1280x720 jpeg) so the backend
  // ROI is independent of the rendered CSS size.
  var calibrating = false;
  var drag = null;          // {sx, sy} (canvas px) while dragging
  var roiPx = null;         // current rectangle in canvas px
  var naturalScale = { x: 1, y: 1 };

  function setCalibrating(on) {
    calibrating = on;
    canvas.hidden = !on;
    roiActs.hidden = !on;
    btnRoi.hidden = on;
    if (on) sizeCanvas();
    else { roiPx = null; clearCanvas(); }
  }
  function sizeCanvas() {
    if (!img.naturalWidth) return;
    var rect = img.getBoundingClientRect();
    canvas.width = rect.width;
    canvas.height = rect.height;
    canvas.style.width = rect.width + 'px';
    canvas.style.height = rect.height + 'px';
    naturalScale.x = img.naturalWidth / rect.width;
    naturalScale.y = img.naturalHeight / rect.height;
  }
  function clearCanvas() {
    if (!canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
  function drawRoi() {
    if (!roiPx) return;
    var ctx = canvas.getContext('2d');
    clearCanvas();
    ctx.strokeStyle = 'rgba(255,255,255,0.95)';
    ctx.lineWidth = 2;
    ctx.fillStyle = 'rgba(56,189,248,0.20)';
    ctx.fillRect(roiPx.x, roiPx.y, roiPx.w, roiPx.h);
    ctx.strokeRect(roiPx.x, roiPx.y, roiPx.w, roiPx.h);
  }
  function pointerXY(ev) {
    var rect = canvas.getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
  }
  canvas.addEventListener('pointerdown', function (ev) {
    if (!calibrating) return;
    canvas.setPointerCapture(ev.pointerId);
    drag = pointerXY(ev);
    roiPx = { x: drag.x, y: drag.y, w: 0, h: 0 };
    drawRoi();
  });
  canvas.addEventListener('pointermove', function (ev) {
    if (!calibrating || !drag) return;
    var p = pointerXY(ev);
    roiPx = {
      x: Math.min(drag.x, p.x), y: Math.min(drag.y, p.y),
      w: Math.abs(p.x - drag.x), h: Math.abs(p.y - drag.y),
    };
    drawRoi();
  });
  canvas.addEventListener('pointerup', function () { drag = null; });

  // Settings panel visibility and the gear highlight are one state — keep them
  // in sync so the gear never stays lit after the panel closes, and entering
  // calibration always un-lights it.
  function setSettingsOpen(open) {
    if (settings) settings.hidden = !open;
    if (btnSettings) btnSettings.classList.toggle('cam-btn-primary', open);
  }
  function setCalibratingAndClose(v) {
    setCalibrating(v);
    if (v) setSettingsOpen(false);  // entering calibration closes the gear panel
  }
  btnRoi.addEventListener('click', function () { setCalibratingAndClose(true); });
  btnCancel.addEventListener('click', function () { setCalibratingAndClose(false); });
  if (btnRoiClearSaved) {
    btnRoiClearSaved.addEventListener('click', async function () {
      try {
        await fetch('/api/camera/roi', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: 'null'});
      } catch (e) {}
      pollStatus();
    });
  }
  btnSave.addEventListener('click', async function () {
    if (!roiPx || roiPx.w < 8 || roiPx.h < 8) return;
    var natural = {
      x: Math.round(roiPx.x * naturalScale.x),
      y: Math.round(roiPx.y * naturalScale.y),
      w: Math.round(roiPx.w * naturalScale.x),
      h: Math.round(roiPx.h * naturalScale.y),
    };
    try {
      var r = await fetch('/api/camera/roi', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(natural),
      });
      if (!r.ok) { console.warn('ROI save failed', r.status); return; }
    } catch (e) { console.warn(e); return; }
    setCalibratingAndClose(false);
  });

  // -- settings panel toggle (gear icon) --------------------------------
  if (btnSettings) {
    btnSettings.addEventListener('click', function () {
      var opening = settings.hidden;
      if (opening && calibrating) setCalibrating(false);  // never show both panels at once
      setSettingsOpen(opening);
    });
  }

  // -- segmented "Auto / En place / Retirée" force-state selector ------
  document.querySelectorAll('.cam-seg').forEach(function (btn) {
    btn.addEventListener('click', async function () {
      var state = btn.getAttribute('data-force');
      try {
        var r = await fetch('/api/camera/cover/state?state=' + state, {method: 'POST'});
        if (!r.ok) { console.warn('force state failed', r.status); return; }
      } catch (e) { console.warn(e); return; }
      pollStatus();
    });
  });
  async function calibrateCover(state) {
    try {
      var r = await fetch('/api/camera/cover/calibrate?state=' + state, {method: 'POST'});
      if (!r.ok) {
        var msg = await r.text();
        if (coverHelp) coverHelp.textContent = 'Erreur ' + r.status + ' — ' + msg.slice(0, 120);
        return;
      }
      pollStatus();   // refresh badge + helper immediately
    } catch (e) {
      if (coverHelp) coverHelp.textContent = 'Erreur réseau';
    }
  }
  if (btnCoverOn)  btnCoverOn .addEventListener('click', function () { calibrateCover('on');  });
  if (btnCoverOff) btnCoverOff.addEventListener('click', function () { calibrateCover('off'); });
  if (btnCoverRst) {
    btnCoverRst.addEventListener('click', async function () {
      try {
        await fetch('/api/camera/cover/reset', {method: 'POST'});
        pollStatus();
      } catch (e) {}
    });
  }

  // -- start loops --
  pollStatus();
  setInterval(pollStatus, 5000);
})();
