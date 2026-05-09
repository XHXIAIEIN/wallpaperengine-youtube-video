// Twitch — channels and VODs. Resolved server-side via yt-dlp.
(window.Platforms = window.Platforms || {}).twitch = {
  name: 'twitch',
  label: 'Twitch',
  match(s) {
    return /twitch\.tv/i.test(s)
        || /^[a-zA-Z0-9_]{3,25}$/.test(s);
  },
};
