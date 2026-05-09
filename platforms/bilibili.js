// Bilibili — BV/av videos and live rooms. Resolved server-side via yt-dlp.
(window.Platforms = window.Platforms || {}).bilibili = {
  name: 'bilibili',
  label: 'Bilibili',
  match(s) {
    return /bilibili\.com/i.test(s) || /^BV[A-Za-z0-9]+$/.test(s) || /^av\d+$/i.test(s);
  },
};
