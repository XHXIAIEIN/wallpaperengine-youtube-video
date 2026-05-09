// Main application: ask helper to transcode the source into VP9/Opus webm,
// pipe it into the native <video> element. WE properties drive state.
// Pause/mute on background or fullscreen is delegated to WE's global
// "性能 → 回放" rules — we don't reimplement them here.
(function () {
  const HELPER = 'http://127.0.0.1:9876';

  const state = {
    url: 'https://www.youtube.com/watch?v=jfKfPfyJRdk',
    localVideo: '',         // local file path; if set, overrides url
    platform: 'auto',
    muted: false,
    loop: true,
    liveCatchup: true,      // for live streams: auto-seek to buffered edge when behind
    height: 1080,           // 0 = source, else hard cap (1080/720/480/360)
    speed: 1.0,
    volume: 0.5,
    alignment: 'cover',
    scale: 1,
    offsetX: 0,             // -100..100 (vw)
    offsetY: 0,             // -100..100 (vh)
    safeBottom: 0,          // 0..15 (vh) — taskbar inset
    showDiag: false,
    isLive: false,          // resolved at runtime from helper
  };

  let catchupTimer = null;

  function applyVisualVars() {
    const v = document.getElementById('player');
    if (!v) return;
    v.style.setProperty('--align', state.alignment);
    v.style.setProperty('--scale', state.scale);
    v.style.setProperty('--ox', state.offsetX + 'vw');
    v.style.setProperty('--oy', state.offsetY + 'vh');
    document.documentElement.style.setProperty('--safe-bottom', state.safeBottom + 'vh');
  }

  function pickPlatform(url) {
    if (state.platform !== 'auto') return Platforms[state.platform] || null;
    for (const k in Platforms) if (Platforms[k].match(url)) return Platforms[k];
    return null;
  }

  async function probeHelper() {
    Diag.set('helper', 'checking ' + HELPER, 'warn');
    const t0 = Date.now();
    try {
      const r = await fetch(HELPER + '/preview.png', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      Diag.set('helper', 'OK ' + (Date.now() - t0) + 'ms', 'ok');
      return true;
    } catch (e) {
      Diag.set('helper', 'FAIL — run install-autostart.bat (' + e.message + ')', 'err');
      return false;
    }
  }

  async function fetchMeta(url) {
    const r = await fetch(HELPER + '/api/resolve?url=' + encodeURIComponent(url)
                          + '&max_height=' + state.height);
    const info = await r.json();
    if (!r.ok) throw new Error(info.error || ('HTTP ' + r.status));
    return info;
  }

  function teardown() {
    const v = document.getElementById('player');
    try { v.pause(); v.removeAttribute('src'); v.load(); } catch (_) {}
    if (catchupTimer) { clearInterval(catchupTimer); catchupTimer = null; }
  }

  // For live streams: if currentTime falls more than LAG_THRESHOLD seconds
  // behind the buffered edge (e.g. after the wallpaper was paused / window
  // hidden), seek forward to within LAG_TARGET seconds of the edge.
  function startCatchupLoop(v) {
    const LAG_THRESHOLD = 6;  // tolerate up to 6s drift
    const LAG_TARGET    = 1.5;
    if (catchupTimer) clearInterval(catchupTimer);
    catchupTimer = setInterval(() => {
      if (!state.isLive || !state.liveCatchup) return;
      if (v.paused || v.readyState < 2) return;
      const buf = v.buffered;
      if (!buf || buf.length === 0) return;
      const edge = buf.end(buf.length - 1);
      const lag  = edge - v.currentTime;
      if (lag > LAG_THRESHOLD) {
        const target = Math.max(v.currentTime, edge - LAG_TARGET);
        Diag.set('catchup', 'lag ' + lag.toFixed(1) + 's → seek +'
                 + (target - v.currentTime).toFixed(1) + 's', 'warn');
        try { v.currentTime = target; } catch (_) {}
      }
    }, 4000);
  }

  async function render() {
    teardown();
    const v = document.getElementById('player');
    v.muted = state.muted;
    v.loop  = state.loop;
    v.playbackRate = state.speed;
    v.volume = state.volume;
    applyVisualVars();

    v.addEventListener('loadedmetadata', () => {
      Diag.set('video', 'metadata ' + v.videoWidth + 'x' + v.videoHeight, 'ok');
    }, { once: true });
    v.addEventListener('playing', () => Diag.set('video',
      'playing ' + v.videoWidth + 'x' + v.videoHeight, 'ok'), { once: true });
    v.onerror = () => {
      const e = v.error;
      Diag.set('video', 'error code=' + (e && e.code) + ' msg=' + (e && e.message), 'err');
    };

    // Local video file overrides URL/helper path entirely.
    if (state.localVideo) {
      Diag.set('platform', 'local file', 'ok');
      Diag.set('url',      state.localVideo, 'ok');
      Diag.set('stream',   'local file (no transcode — VP8/VP9/Opus only)', 'warn');
      Diag.set('codec',    'native', 'ok');
      v.src = state.localVideo;
      v.addEventListener('loadedmetadata', () => { v.playbackRate = state.speed; }, { once: true });
      v.play().catch(e => Diag.set('play', 'autoplay blocked: ' + e.message, 'warn'));
      return;
    }

    const p = pickPlatform(state.url);
    Diag.set('platform', p ? p.label : 'unknown (sending to helper anyway)', p ? 'ok' : 'warn');
    Diag.set('url', state.url, 'ok');

    if (!await probeHelper()) return;

    Diag.set('stream', 'resolving metadata...', 'warn');
    let info;
    try {
      info = await fetchMeta(state.url);
    } catch (e) {
      Diag.set('stream', 'resolve failed: ' + e.message, 'err');
      return;
    }
    state.isLive = !!info.is_live;
    Diag.set('title',  info.title || '(no title)', 'ok');
    Diag.set('stream', info.kind + (info.is_live ? ' / LIVE' : '') +
                       (info.height ? ' / ' + info.height + 'p source' : ''), 'ok');
    Diag.set('codec',  'VP9/Opus @ ' + (state.height === 0 ? 'source' : state.height + 'p'), 'ok');

    const bitrateFor = { 0: '5500k', 1080: '4500k', 720: '2500k', 480: '1200k', 360: '700k' };
    const transcodeUrl = HELPER + '/api/transcode'
        + '?url=' + encodeURIComponent(state.url)
        + '&h='   + state.height
        + '&vb='  + (bitrateFor[state.height] || '2500k')
        + '&_t='  + Date.now();

    v.src = transcodeUrl;
    // Reapply playbackRate after src change (Chromium resets it on reload).
    v.addEventListener('loadedmetadata', () => { v.playbackRate = state.speed; }, { once: true });
    v.play().catch(e => Diag.set('play', 'autoplay blocked: ' + e.message, 'warn'));
    if (state.isLive) startCatchupLoop(v);
  }

  // ---- WE props ----
  function applyProps(props) {
    let needsRender = false;

    if (props.videourl && typeof props.videourl.value === 'string') {
      const v = props.videourl.value.trim();
      if (v && v !== state.url) { state.url = v; needsRender = true; }
    }
    if (props.localvideo && typeof props.localvideo.value === 'string') {
      const lv = props.localvideo.value.trim();
      if (lv !== state.localVideo) { state.localVideo = lv; needsRender = true; }
    }
    if (props.platform && props.platform.value !== state.platform) {
      state.platform = props.platform.value; needsRender = true;
    }
    if (props.height) {
      const h = parseInt(props.height.value, 10);
      if (!isNaN(h) && h !== state.height) { state.height = h; needsRender = true; }
    }

    const vel = document.getElementById('player');
    if (props.muted) {
      const v = !!props.muted.value;
      if (v !== state.muted) { state.muted = v; if (vel) vel.muted = v; }
    }
    if (props.loop) {
      const v = !!props.loop.value;
      if (v !== state.loop) { state.loop = v; if (vel) vel.loop = v; }
    }
    if (props.livecatchup) {
      state.liveCatchup = !!props.livecatchup.value;
    }
    if (props.speed) {
      const s = +props.speed.value;
      if (!isNaN(s) && s > 0 && s !== state.speed) {
        state.speed = s;
        if (vel) vel.playbackRate = s;
      }
    }
    if (props.audiovolume) {
      const vol = (+props.audiovolume.value) / 100;
      if (!isNaN(vol) && vol !== state.volume) {
        state.volume = Math.max(0, Math.min(1, vol));
        if (vel) vel.volume = state.volume;
      }
    }

    let visualDirty = false;
    if (props.alignment && props.alignment.value !== state.alignment) {
      state.alignment = props.alignment.value; visualDirty = true;
    }
    if (props.scale) {
      const x = (+props.scale.value) / 100;
      if (!isNaN(x) && x !== state.scale) { state.scale = x; visualDirty = true; }
    }
    if (props.offsetx) {
      const x = +props.offsetx.value;
      if (!isNaN(x) && x !== state.offsetX) { state.offsetX = x; visualDirty = true; }
    }
    if (props.offsety) {
      const x = +props.offsety.value;
      if (!isNaN(x) && x !== state.offsetY) { state.offsetY = x; visualDirty = true; }
    }
    if (props.safebottom) {
      const x = +props.safebottom.value;
      if (!isNaN(x) && x !== state.safeBottom) { state.safeBottom = x; visualDirty = true; }
    }
    if (visualDirty) applyVisualVars();

    if (props.showdiag) Diag.toggle(!!props.showdiag.value);

    if (needsRender) render();
  }

  window.wallpaperPropertyListener = {
    applyUserProperties(props) { applyProps(props); },
  };

  window.addEventListener('DOMContentLoaded', () => { Diag.init(); render(); });

  ['click', 'mousedown', 'contextmenu', 'wheel', 'keydown']
    .forEach(ev => window.addEventListener(ev, e => e.stopPropagation(), true));
})();
