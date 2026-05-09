// Bottom-right diagnostic panel + helper-backed log sink (so we can read logs from disk).
(function () {
  const HELPER = 'http://127.0.0.1:9876';
  let el, lines = {}, anyFail = false;

  function flush() {
    el.innerHTML = Object.keys(lines).map(k =>
      '<span class="k">' + k + '</span>' + lines[k]
    ).join('');
  }

  function post(payload) {
    try {
      const data = JSON.stringify(Object.assign({ t: Date.now() }, payload));
      const blob = new Blob([data], { type: 'application/json' });
      if (navigator.sendBeacon) navigator.sendBeacon(HELPER + '/api/log', blob);
      else fetch(HELPER + '/api/log', { method: 'POST', body: data, keepalive: true });
    } catch (_) {}
  }

  function probeCodecs() {
    const tests = [
      // HLS audio+video container is fmp4/ts wrapping H.264 + AAC.
      ['video/mp4; codecs="avc1.42C015"',          'H.264 Baseline 144p'],
      ['video/mp4; codecs="avc1.4D401E"',          'H.264 Main 360p'],
      ['video/mp4; codecs="avc1.4D401F"',          'H.264 Main 720p'],
      ['video/mp4; codecs="avc1.640028"',          'H.264 High 1080p'],
      ['video/mp4; codecs="mp4a.40.2"',            'AAC-LC'],
      ['video/mp4; codecs="mp4a.40.5"',            'HE-AAC'],
      ['video/mp4; codecs="avc1.42C015,mp4a.40.2"', 'AVC+AAC combined'],
      ['application/vnd.apple.mpegurl',            'native HLS'],
    ];
    const results = {};
    const v = document.createElement('video');
    for (const [type, label] of tests) {
      const ms = (window.MediaSource && MediaSource.isTypeSupported(type)) ? 'MS' : '';
      const native = v.canPlayType(type);
      results[label] = (ms ? '[MS] ' : '') + (native || '(no native)');
    }
    return results;
  }

  window.Diag = {
    init() {
      el = document.getElementById('diag');
      el.classList.add('hide');   // hidden by default; any 'err' status auto-reveals.
      this.set('origin', location.origin || '(null)', 'ok');
      this.set('chrome', (navigator.userAgent.match(/Chrome\/([\d.]+)/) || [])[1] || '?', 'ok');
      this.set('online', navigator.onLine ? 'yes' : 'no', navigator.onLine ? 'ok' : 'err');
      const codecs = probeCodecs();
      post({ event: 'boot', ua: navigator.userAgent, codecs });
      this.set('codecs', JSON.stringify(codecs).slice(0, 80) + '…', 'ok');
    },

    set(key, msg, cls) {
      const safe = String(msg)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      lines[key] = '<span class="v ' + (cls || '') + '" title="'
                 + safe.replace(/"/g, '&quot;') + '">' + safe + '</span>';
      if (cls === 'err') { anyFail = true; el.classList.remove('hide'); }
      flush();
      post({ event: 'diag', key, msg, cls });
    },

    probe(url, key) {
      if (!url) return;
      const k = 'probe.' + key;
      this.set(k, 'pinging...', 'warn');
      const img = new Image();
      const t0 = Date.now();
      const timer = setTimeout(() => this.set(k, 'TIMEOUT', 'err'), 10000);
      img.onload  = () => { clearTimeout(timer); this.set(k, 'OK ' + (Date.now() - t0) + 'ms', 'ok'); };
      img.onerror = () => { clearTimeout(timer); this.set(k, 'FAIL', 'err'); };
      img.src = url + '?_t=' + Date.now();
    },

    toggle(on) {
      if (on) el.classList.remove('hide');
      else if (!anyFail) el.classList.add('hide');
    },

    log(event, payload) { post(Object.assign({ event }, payload || {})); },
  };
})();
