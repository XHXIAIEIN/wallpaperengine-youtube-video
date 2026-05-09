// Vimeo — Resolved server-side via yt-dlp.
(window.Platforms = window.Platforms || {}).vimeo = {
  name: 'vimeo',
  label: 'Vimeo',
  match(s) { return /vimeo\.com/i.test(s); },
};
