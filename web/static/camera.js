/* Camera card: live snapshot refresh.

   We poll /api/camera/status independently from the chart's own /history poll:
   fewer races, simpler retry logic. */

(function () {
  'use strict';

  // -- DOM --
  var card    = document.getElementById('cam-card');
  if (!card) return;             // server didn't render the card; subsystem is off
  var img     = document.getElementById('cam-img');
  var empty   = document.getElementById('cam-empty');
  var meta    = document.getElementById('cam-meta');
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

  // -- /api/camera/status loop --
  async function pollStatus() {
    try {
      var r = await fetch('/api/camera/status');
      if (!r.ok) return;
      var snap = await r.json();
      if (!snap.enabled) return;
      updateImage(snap);
      updateMeta(snap);
    } catch (e) { /* network blip; try next tick */ }
  }

  // -- start loops --
  pollStatus();
  setInterval(pollStatus, 5000);
})();
