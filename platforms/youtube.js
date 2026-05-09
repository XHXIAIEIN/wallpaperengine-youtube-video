// YouTube — videos, live, playlists, shorts. Resolved server-side via yt-dlp.
(window.Platforms = window.Platforms || {}).youtube = {
  name: 'youtube',
  label: 'YouTube',
  match(s) {
    return /youtube\.com|youtu\.be/i.test(s)
        || /^[A-Za-z0-9_-]{11}$/.test(s);
  },
};
