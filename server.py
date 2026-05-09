"""Helper server for the WE wallpaper.

Two jobs:
  /api/resolve?url=...      Run yt-dlp, return { kind, manifest_or_url, title, is_live }.
  /api/proxy?u=<b64url>     Fetch googlevideo content with proper UA. If response is an
                            HLS playlist, rewrite all child URLs to point back through
                            this proxy so the browser stays inside our origin.

Plus static file serving for index.html, lib/hls.min.js, etc.
"""
import base64
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

PORT = 9876
ROOT = os.path.dirname(os.path.abspath(__file__))
YTDLP = "yt-dlp"   # rely on PATH; override via env if needed
FFMPEG = "ffmpeg"  # ditto
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

# Hide the console window when pythonw spawns yt-dlp.exe on Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# In-memory resolve cache. googlevideo URLs live ~6h; we expire well before that.
_CACHE: dict = {}
_CACHE_TTL = 3600  # 1 hour
_CACHE_LOCK = threading.Lock()

# pythonw has no console; redirect to a log so print() never crashes.
try:
    if sys.stdout is None or not getattr(sys.stdout, "writable", lambda: False)():
        raise RuntimeError("no stdout")
except Exception:
    log = open(os.path.join(ROOT, "server.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log


def b64url_enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def b64url_dec(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii")).decode("utf-8")


_YT_RE        = re.compile(r"(?:youtube\.com|youtu\.be|m\.youtube\.com)", re.I)
_BILI_LIVE_RE = re.compile(r"live\.bilibili\.com/(?:h5/)?(\d+)", re.I)
_BILI_VID_RE  = re.compile(r"\b(BV[A-Za-z0-9]{10}|av\d+)\b", re.I)


def resolve(url: str, max_height: int = 720) -> dict:
    """Platform-dispatched resolver. yt-dlp is YouTube-only; other platforms
    use their public APIs directly.
    """
    # Bilibili live — native API.
    if _BILI_LIVE_RE.search(url):
        return resolve_bilibili_live(url)
    # Bilibili video (BV/av) — native API.
    if _BILI_VID_RE.search(url):
        return resolve_bilibili_video(url, max_height)
    # YouTube — yt-dlp (signature decryption requires it).
    if _YT_RE.search(url):
        return resolve_youtube_with_ytdlp(url, max_height)
    # Bilibili domain but no room id / BV id — homepage or directory page.
    if "bilibili.com" in url.lower():
        raise RuntimeError(
            "Bilibili URL has no room/video ID — paste a specific room "
            "(e.g. https://live.bilibili.com/12345) or a video page "
            "(e.g. https://www.bilibili.com/video/BVxxxxxxxxxx)."
        )
    raise RuntimeError("unsupported URL — only YouTube and Bilibili are "
                       "wired up. Add a native resolver for this platform.")


def resolve_bilibili_live(url: str) -> dict | None:
    """Hit Bilibili's open playUrl API directly — far cheaper than yt-dlp.

    Returns None if the URL doesn't look like a live room (caller should fall
    back to yt-dlp). Raises on API errors so callers see them.
    """
    m = _BILI_LIVE_RE.search(url)
    if not m:
        return None
    room_id = m.group(1)

    # Resolve room_id → real_id (short rooms forward to a different cid).
    info_url = ("https://api.live.bilibili.com/room/v1/Room/get_info?id=" + room_id)
    req = urllib.request.Request(info_url, headers={
        "User-Agent": UA,
        "Referer": "https://live.bilibili.com/",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        info = json.loads(r.read().decode("utf-8", "replace"))
    if info.get("code") != 0:
        raise RuntimeError("bilibili get_info failed: " + str(info.get("message")))
    real_id = info["data"]["room_id"]
    title   = info["data"].get("title") or ""
    is_live = info["data"].get("live_status") == 1

    # qn=10000 = original; codec=0 (AVC) for ffmpeg-friendliness; format=2 (fmp4)
    # which serves through CDN m3u8 with proper byte ranges.
    play_url = ("https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
                "?room_id=" + str(real_id) +
                "&protocol=0,1&format=0,1,2&codec=0,1&qn=10000&platform=web&ptype=8")
    req = urllib.request.Request(play_url, headers={
        "User-Agent": UA,
        "Referer": "https://live.bilibili.com/" + str(real_id),
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        play = json.loads(r.read().decode("utf-8", "replace"))
    if play.get("code") != 0:
        raise RuntimeError("bilibili playUrl failed: " + str(play.get("message")))

    streams = (((play.get("data") or {}).get("playurl_info") or {})
               .get("playurl") or {}).get("stream") or []

    # Preference: HLS-fmp4 > HLS-ts > FLV. AVC > HEVC (ffmpeg handles both, but
    # AVC needs less CPU on the libvpx-vp9 re-encode side).
    def _pick():
        prefs = [("http_hls", "fmp4"), ("http_hls", "ts"), ("http_stream", "flv")]
        for proto, fmt in prefs:
            for s in streams:
                if s.get("protocol_name") != proto:
                    continue
                for f in s.get("format", []):
                    if f.get("format_name") != fmt:
                        continue
                    codecs = sorted(f.get("codec", []),
                                    key=lambda c: 0 if c.get("codec_name") == "avc" else 1)
                    for c in codecs:
                        infos = c.get("url_info") or []
                        if not infos:
                            continue
                        host = infos[0].get("host", "")
                        extra = infos[0].get("extra", "")
                        base = c.get("base_url", "")
                        return host + base + extra, fmt, c.get("codec_name")
        return None

    picked = _pick()
    if not picked:
        if not is_live:
            raise RuntimeError("room is offline")
        raise RuntimeError("no playable stream variant")
    stream_url, fmt, codec = picked

    return {
        "kind":    "hls" if fmt in ("fmp4", "ts") else "flv",
        "url":     stream_url,
        "title":   title,
        "is_live": is_live,
        "height":  None,
        "headers": {"Referer": "https://live.bilibili.com/"},
        "source":  "bilibili-api(" + fmt + "/" + (codec or "?") + ")",
    }


def resolve_bilibili_video(url: str, max_height: int = 720) -> dict:
    """Bilibili VOD via the public playurl API. Returns a DASH split (video +
    audio URLs) so the transcode stage muxes them itself — fnval=16 gives us
    AVC + AAC at usable bitrates. fnval=1 progressive MP4 is the fallback.
    """
    m = _BILI_VID_RE.search(url)
    if not m:
        raise RuntimeError("not a bilibili video URL")
    vid = m.group(1)
    if vid.lower().startswith("av"):
        param = "aid=" + vid[2:]
    else:
        param = "bvid=" + vid

    info_req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/view?" + param,
        headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/",
                 "Accept": "application/json"})
    with urllib.request.urlopen(info_req, timeout=15) as r:
        info = json.loads(r.read().decode("utf-8", "replace"))
    if info.get("code") != 0:
        raise RuntimeError("bilibili view failed: " + str(info.get("message")))
    bvid  = info["data"]["bvid"]
    cid   = info["data"]["cid"]
    title = info["data"].get("title") or ""

    # qn=120 → 4K when available; fnval=16 → DASH; fourk=1 → unlock 4K.
    play_req = urllib.request.Request(
        "https://api.bilibili.com/x/player/playurl?bvid={0}&cid={1}"
        "&qn=120&fnval=16&fourk=1".format(bvid, cid),
        headers={"User-Agent": UA,
                 "Referer": "https://www.bilibili.com/video/" + bvid,
                 "Accept": "application/json"})
    with urllib.request.urlopen(play_req, timeout=15) as r:
        play = json.loads(r.read().decode("utf-8", "replace"))
    if play.get("code") != 0:
        raise RuntimeError("bilibili playurl failed: " + str(play.get("message")))

    headers = {"Referer": "https://www.bilibili.com/"}
    dash = (play.get("data") or {}).get("dash")
    if dash and dash.get("video"):
        # Prefer AVC (cheaper for libvpx-vp9 re-encode), then highest height
        # within max_height (or any if max_height==0).
        def _avc_first(c):
            return 0 if "avc" in (c.get("codecs") or "").lower() else 1

        vids = sorted(dash["video"], key=lambda v: (_avc_first(v), -(v.get("height") or 0)))
        if max_height:
            in_range = [v for v in vids if (v.get("height") or 0) <= max_height]
            if in_range:
                vids = in_range
        chosen_v = vids[0]
        # Audio: pick highest bandwidth.
        chosen_a = sorted(dash.get("audio") or [],
                          key=lambda a: a.get("bandwidth") or 0, reverse=True)
        chosen_a = chosen_a[0] if chosen_a else None

        return {
            "kind":      "dash",
            "url":       chosen_v["baseUrl"],
            "audio_url": chosen_a["baseUrl"] if chosen_a else None,
            "title":     title,
            "is_live":   False,
            "height":    chosen_v.get("height"),
            "headers":   headers,
            "source":    "bilibili-api(dash/" + (chosen_v.get("codecs") or "?") + ")",
        }

    # Progressive MP4 fallback (older videos / users without DASH access).
    durl = (play.get("data") or {}).get("durl")
    if durl:
        return {
            "kind":    "mp4",
            "url":     durl[0]["url"],
            "title":   title,
            "is_live": False,
            "height":  (play.get("data") or {}).get("quality"),
            "headers": headers,
            "source":  "bilibili-api(mp4)",
        }
    raise RuntimeError("no playable bilibili video format")


def resolve_youtube_with_ytdlp(url: str, max_height: int = 720) -> dict:
    """YouTube only — pick the best HLS or progressive MP4 variant.

    yt-dlp is overkill for most platforms but YouTube's signature scheme
    requires it. Other platforms have their own native resolvers above.
    """
    cache_key = (url, max_height)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit and hit[0] > now:
            return hit[1]

    cmd = [YTDLP, "-j", "--no-warnings", "--no-playlist", url]
    out = subprocess.check_output(
        cmd, timeout=30, stderr=subprocess.STDOUT, creationflags=NO_WINDOW
    )
    info = json.loads(out.decode("utf-8", errors="replace"))

    is_live = bool(info.get("is_live"))
    title = info.get("title") or ""
    formats = info.get("formats", [])

    # max_height = 0 means "no cap, source quality".
    # Bilibili live reports height=None — treat unknown as in-range so it isn't
    # silently filtered out.
    def _within(f):
        if max_height == 0:
            return True
        h = f.get("height")
        if h is None:
            return True
        return h <= max_height

    # ffmpeg uses `-map 0:a:0?` (optional audio), so we accept formats whose
    # acodec field is missing — bilibili live in particular reports acodec=None
    # even though the muxed stream carries audio.
    def _has_video(f):
        return f.get("vcodec") not in (None, "none")

    # Prefer HLS (m3u8) — works for live + VOD; ffmpeg consumes it directly.
    hls = [f for f in formats
           if "m3u8" in str(f.get("protocol", ""))
           and _has_video(f)
           and _within(f)]
    result = None
    if hls:
        hls.sort(key=lambda f: f.get("height") or 0, reverse=True)
        chosen = hls[0]
        result = {"kind": "hls", "url": chosen["url"], "title": title,
                  "is_live": is_live, "height": chosen.get("height")}

    # FLV fallback (bilibili live serves FLV alongside HLS — it's a valid
    # ffmpeg input even though browsers can't play it natively).
    if result is None:
        flv = [f for f in formats
               if f.get("ext") == "flv"
               and _has_video(f)
               and _within(f)]
        if flv:
            flv.sort(key=lambda f: f.get("height") or 0, reverse=True)
            chosen = flv[0]
            result = {"kind": "flv", "url": chosen["url"], "title": title,
                      "is_live": is_live, "height": chosen.get("height")}

    if result is None:
        # Last resort: progressive mp4 (older VOD). Native <video> can play directly.
        prog = [f for f in formats
                if f.get("ext") == "mp4"
                and _has_video(f)
                and "m3u8" not in str(f.get("protocol", ""))
                and _within(f)]
        if prog:
            prog.sort(key=lambda f: f.get("height") or 0, reverse=True)
            chosen = prog[0]
            result = {"kind": "mp4", "url": chosen["url"], "title": title,
                      "is_live": is_live, "height": chosen.get("height")}

    if result is None:
        raise RuntimeError("no playable format found")

    with _CACHE_LOCK:
        _CACHE[cache_key] = (now + _CACHE_TTL, result)
    return result


def rewrite_m3u8(text: str, base: str, proxy_prefix: str) -> str:
    """Rewrite every URI line / KEY URI / MAP URI in the playlist to go through us."""
    out_lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out_lines.append(line)
            continue
        if s.startswith("#"):
            # Rewrite URI="..." attributes inside tags (EXT-X-KEY, EXT-X-MAP, EXT-X-MEDIA, etc.).
            out_lines.append(_rewrite_attr_uris(line, base, proxy_prefix))
            continue
        # Plain URI line (segment or sub-playlist).
        absolute = urllib.parse.urljoin(base, s)
        out_lines.append(proxy_prefix + b64url_enc(absolute))
    return "\n".join(out_lines) + "\n"


def _rewrite_attr_uris(line: str, base: str, proxy_prefix: str) -> str:
    key = 'URI="'
    out = []
    i = 0
    while True:
        j = line.find(key, i)
        if j < 0:
            out.append(line[i:])
            break
        out.append(line[i:j + len(key)])
        end = line.find('"', j + len(key))
        if end < 0:
            out.append(line[j + len(key):])
            break
        original = line[j + len(key):end]
        absolute = urllib.parse.urljoin(base, original)
        out.append(proxy_prefix + b64url_enc(absolute))
        out.append('"')
        i = end + 1
    return "".join(out)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))

    # Static files: allow CORS so file:// can fetch us.
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/resolve"):
            return self._handle_resolve()
        if self.path.startswith("/api/proxy"):
            return self._handle_proxy()
        if self.path.startswith("/api/transcode"):
            return self._handle_transcode()
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/log"):
            return self._handle_log()
        self.send_response(404)
        self.end_headers()

    # ---- /api/log ---- client posts JSON; we append to client.log
    def _handle_log(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            payload = {"raw": body.decode("utf-8", errors="replace")}
        path = os.path.join(ROOT, "client.log")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            return self._json(500, {"error": str(e)})
        return self._json(200, {"ok": True})

    # ---- /api/resolve ----
    def _handle_resolve(self):
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        url = params.get("url", [""])[0]
        try:
            max_h = int(params.get("max_height", ["720"])[0])
        except ValueError:
            max_h = 720
        if not url:
            return self._json(400, {"error": "missing url"})
        try:
            info = resolve(url, max_height=max_h)
        except subprocess.CalledProcessError as e:
            return self._json(502, {
                "error": "yt-dlp failed",
                "stderr": e.output.decode("utf-8", errors="replace")[-800:],
            })
        except Exception as e:
            return self._json(500, {"error": str(e)})

        # Hand back a proxy URL — the browser never sees googlevideo directly.
        # Don't mutate the cached dict: build a fresh response.
        out = {k: v for k, v in info.items() if k != "url"}
        out["proxy"] = "/api/proxy?u=" + b64url_enc(info["url"])
        return self._json(200, out)

    # ---- /api/transcode ---- spawn ffmpeg, pipe webm/VP9+Opus to the browser.
    def _handle_transcode(self):
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        url = params.get("url", [""])[0]
        try:
            max_h = int(params.get("h", ["720"])[0])
        except ValueError:
            max_h = 720
        try:
            bitrate = params.get("vb", ["2500k"])[0]
        except Exception:
            bitrate = "2500k"
        if not url:
            return self._json(400, {"error": "missing url"})

        try:
            info = resolve(url, max_height=max_h)
        except Exception as e:
            return self._json(502, {"error": "resolve failed: " + str(e)})

        vf_args = ["-vf", "scale=-2:" + str(max_h)] if max_h > 0 else []

        # Per-input headers (Referer-protected sources like Bilibili CDN).
        # -user_agent and -headers are per-input http options; reapply before
        # each -i so input 1 (audio) gets them too, not just input 0.
        hdrs = info.get("headers") or {}
        h_str = "".join("%s: %s\r\n" % (k, v) for k, v in hdrs.items()) if hdrs else None

        def _input_opts():
            opts = ["-user_agent", UA]
            if h_str:
                opts += ["-headers", h_str]
            return opts

        ffmpeg_cmd = [FFMPEG, "-hide_banner", "-loglevel", "warning"]
        ffmpeg_cmd += _input_opts()
        ffmpeg_cmd += ["-i", info["url"]]

        if info.get("audio_url"):
            ffmpeg_cmd += _input_opts()
            ffmpeg_cmd += ["-i", info["audio_url"]]
            ffmpeg_cmd += ["-map", "0:v:0", "-map", "1:a:0?"]
        else:
            ffmpeg_cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

        ffmpeg_cmd += [
            *vf_args,
            "-c:v", "libvpx-vp9",
            "-b:v", bitrate, "-deadline", "realtime", "-cpu-used", "8",
            "-row-mt", "1", "-tile-columns", "2", "-frame-parallel", "1",
            "-c:a", "libopus", "-b:a", "128k",
            "-f", "webm", "-live", "1",
            "-flush_packets", "1",
            "pipe:1",
        ]

        try:
            proc = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=NO_WINDOW,
            )
        except Exception as e:
            return self._json(500, {"error": "ffmpeg spawn failed: " + str(e)})

        # Drain stderr in a thread so the pipe never blocks.
        def _drain_stderr():
            for line in proc.stderr:
                try:
                    sys.stdout.write("[ffmpeg] " + line.decode("utf-8", "replace"))
                except Exception:
                    pass
        threading.Thread(target=_drain_stderr, daemon=True).start()

        # No Content-Length, no Range support — pure live stream.
        self.send_response(200)
        self.send_header("Content-Type", "video/webm")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    # ---- /api/proxy ----
    def _handle_proxy(self):
        q = urllib.parse.urlparse(self.path).query
        token = urllib.parse.parse_qs(q).get("u", [""])[0]
        if not token:
            return self._json(400, {"error": "missing u"})
        try:
            target = b64url_dec(token)
        except Exception:
            return self._json(400, {"error": "bad token"})

        # Build upstream request. Range support for segments.
        req = urllib.request.Request(target, headers={
            "User-Agent": UA,
            "Accept": "*/*",
        })
        rng = self.headers.get("Range")
        if rng:
            req.add_header("Range", rng)

        try:
            up = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            try:
                self.wfile.write(e.read())
            except Exception:
                pass
            return
        except Exception as e:
            return self._json(502, {"error": "upstream: " + str(e)})

        ctype = up.headers.get("Content-Type", "")
        # If it's an HLS playlist, rewrite child URLs.
        is_m3u8 = ("mpegurl" in ctype.lower()) or target.split("?", 1)[0].endswith(".m3u8")
        if is_m3u8:
            body = up.read().decode("utf-8", errors="replace")
            rewritten = rewrite_m3u8(body, base=target, proxy_prefix="/api/proxy?u=")
            data = rewritten.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # Otherwise: streaming pass-through (for .ts segments, .mp4, etc.).
        self.send_response(up.status)
        for h in ("Content-Type", "Content-Length", "Content-Range",
                  "Accept-Ranges", "Cache-Control"):
            v = up.headers.get(h)
            if v:
                self.send_header(h, v)
        self.end_headers()
        try:
            while True:
                chunk = up.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    # ---- helpers ----
    def _json(self, code: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print("serving", ROOT, "on", port)
    try:
        with ThreadingServer(("127.0.0.1", port), Handler) as s:
            s.serve_forever()
    except OSError as e:
        print("bind failed:", e)
        raise
