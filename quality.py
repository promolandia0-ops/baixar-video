#!/usr/bin/env python3
"""Busca ampla por vídeos em melhor qualidade (4K/1080p), sem filtro de idade.

Safe search desligado (o usuário é adulto; conteúdo adulto legal é permitido).
Sites de vazamento e pirataria entram na blocklist e são descartados.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests
from yt_dlp.utils import DownloadError

# Importado de app para reutilizar yt-dlp / cookies / impersonate.
# Evita import circular: as funções são usadas só em tempo de chamada.

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# Agregadores de vazamento e sites de pirataria (bloqueados de propósito).
BLOCK_HOSTS = (
    "leakgallery", "leakedzone", "leakedmodels", "leaked.xxx", "leaknudes",
    "thothub", "coomer.su", "coomer.party", "kemono.su", "kemono.party",
    "simpcity", "nudostar", "fapello", "thenude", "celebrityleaks",
    "socialmediagirls", "bunkr-leak", "hotleak", "leak.xxx",
    "thepiratebay", "1337x", "1377x", "rarbg", "yts.mx", "yts.lt", "yify",
    "fitgirl", "pirate-bay", "limetorrents", "torrentz", "nyaa.si",
)

_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 12 * 60  # segundos


def _norm_host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    if h.startswith("www."):
        h = h[4:]
    return h


def blocked(url: str) -> bool:
    h = _norm_host(url)
    if not h:
        return True
    return any(h == b or h.endswith("." + b) or b in h for b in BLOCK_HOSTS)



def data_for_url(url: str, data: dict | None) -> dict | None:
    """Escolhe os cookies do domínio de `url` no cookie_jar enviado pelo app.
    Os cookies vêm só nesta requisição; nada é gravado no servidor.
    Cookies de um site não são enviados para outro.
    """
    if not data:
        return None
    out = {k: v for k, v in data.items() if k != "cookie_jar"}
    jar = data.get("cookie_jar") or {}
    if not isinstance(jar, dict):
        jar = {}
    h = _norm_host(url)
    picked = None
    best = -1
    for key, val in jar.items():
        if not val:
            continue
        key_s = str(key)
        kh = _norm_host(key_s if "://" in key_s else "https://" + key_s)
        if not kh or not h:
            continue
        if h == kh or h.endswith("." + kh) or kh.endswith("." + h):
            if len(kh) > best:
                best = len(kh)
                picked = val
    if picked:
        out["cookies"] = picked
    elif out.get("cookies"):
        ref_h = _norm_host(str(data.get("referer") or ""))
        if not ref_h or not (h == ref_h or h.endswith("." + ref_h) or ref_h.endswith("." + h)):
            out.pop("cookies", None)
    return out


def site_name(url: str) -> str:
    h = _norm_host(url)
    if not h:
        return ""
    if "youtube" in h or h == "youtu.be":
        return "YouTube"
    if "vimeo" in h:
        return "Vimeo"
    if "dailymotion" in h:
        return "Dailymotion"
    if "archive.org" in h:
        return "Archive.org"
    if "reddit" in h:
        return "Reddit"
    parts = h.split(".")
    return parts[-2].capitalize() if len(parts) >= 2 else h


def normalize_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\[\](){}|·•]", " ", s)
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def title_similarity(a: str, b: str) -> float:
    """Parecido com Jaccard de tokens + cobertura das palavras maiores."""
    ta = set(normalize_title(a).split())
    tb = set(normalize_title(b).split())
    stop = {"the", "a", "an", "o", "os", "a", "as", "de", "da", "do", "e", "and", "of", "in", "on", "official", "video", "video", "oficial"}
    ta -= stop
    tb -= stop
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    jacc = inter / len(ta | tb)
    cov = inter / max(1, min(len(ta), len(tb)))
    return max(jacc, cov * 0.85 + jacc * 0.15)


def same_video(src_title: str, src_dur: float | None, cand_title: str, cand_dur: float | None, min_sim: float = 0.45) -> bool:
    sim = title_similarity(src_title, cand_title)
    if sim < min_sim:
        return False
    if src_dur and cand_dur and src_dur > 0 and cand_dur > 0:
        if abs(float(src_dur) - float(cand_dur)) > 3.5:
            return False
    elif src_dur and cand_dur is None:
        return sim >= 0.7
    return True


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9,pt-BR,pt;q=0.8",
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    })
    # Safe search desligado no Bing (além do parâmetro adlt=off).
    s.cookies.set("SRCHHPGUSR", "ADLT=OFF", domain=".bing.com")
    return s


def _unwrap_ddg(href: str) -> str:
    href = unescape(href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        p = urlparse(href)
        qs = parse_qs(p.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
        if "u" in qs and "duckduckgo" in (p.netloc or ""):
            return unquote(qs["u"][0])
    except Exception:
        pass
    return href


def _unwrap_bing(href: str) -> str:
    href = unescape(href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    try:
        p = urlparse(href)
        qs = parse_qs(p.query)
        u = (qs.get("u") or [None])[0]
        if u:
            u = unquote(u)
            if u.startswith("http"):
                return u
            if re.match(r"^a1[A-Za-z0-9+/_=-]+$", u):
                raw = u[2:] + "=" * ((4 - len(u[2:]) % 4) % 4)
                try:
                    import base64
                    dec = base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
                    if dec.startswith("http"):
                        return dec
                except Exception:
                    pass
    except Exception:
        pass
    return href


def _is_search_host(url: str) -> bool:
    h = _norm_host(url)
    return h.endswith("bing.com") or h.endswith("duckduckgo.com") or h.endswith("yahoo.com") or h.endswith("google.com")


def search_duckduckgo(query: str, offset: int = 0) -> list[dict]:
    """HTML do DuckDuckGo, safe search desligado (kp=-2)."""
    out: list[dict] = []
    try:
        s = _session()
        params = {"q": query, "kp": "-2", "kl": "wt-wt"}
        if offset:
            params["s"] = str(offset)
        r = s.post("https://html.duckduckgo.com/html/", data=params, timeout=18)
        r.raise_for_status()
        html = r.text
        if "result__a" not in html and "anomaly" in html.lower():
            r = s.get("https://html.duckduckgo.com/html/", params=params, timeout=18)
            r.raise_for_status()
            html = r.text
        for m in re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html,
            re.I | re.S,
        ):
            url = _unwrap_ddg(m.group(1))
            title = re.sub(r"<[^>]+>", "", unescape(m.group(2))).strip()
            if url.startswith("http") and not blocked(url):
                out.append({"url": url, "title": title, "source": "ddg"})
        if not out:
            for m in re.finditer(r'href="(https?://[^"]+)"[^>]*>([^<]{8,160})</a>', html, re.I):
                url = _unwrap_ddg(m.group(1))
                if "duckduckgo.com" in url:
                    continue
                if url.startswith("http") and not blocked(url):
                    out.append({"url": url, "title": unescape(m.group(2)).strip(), "source": "ddg"})
    except Exception:
        pass
    return out


def search_bing(query: str, offset: int = 0) -> list[dict]:
    """HTML do Bing, safe search desligado (adlt=off)."""
    out: list[dict] = []
    try:
        s = _session()
        params = {"q": query, "adlt": "off"}
        if offset:
            params["first"] = str(offset + 1)
        r = s.get("https://www.bing.com/search", params=params, timeout=18)
        r.raise_for_status()
        html = r.text
        for m in re.finditer(
            r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html,
            re.I,
        ):
            url = _unwrap_bing(m.group(1))
            title = re.sub(r"<[^>]+>", "", unescape(m.group(2))).strip()
            if url.startswith("http") and not blocked(url) and not _is_search_host(url):
                out.append({"url": url, "title": title, "source": "bing"})
    except Exception:
        pass
    return out


def search_bing_videos(query: str, offset: int = 0) -> list[dict]:
    """Página de vídeos do Bing (safe search off). Inclui título, url e miniatura."""
    out: list[dict] = []
    try:
        s = _session()
        params = {"q": query, "adlt": "off"}
        if offset:
            params["first"] = str(offset + 1)
        r = s.get("https://www.bing.com/videos/search", params=params, timeout=18)
        r.raise_for_status()
        text = unescape(r.text)
        seen: set[str] = set()
        # blocos com vt (título) e pgurl/murl (página do vídeo)
        for m in re.finditer(
            r'"vt"\s*:\s*"([^"]{3,240})".{0,500}?"(?:pgurl|murl)"\s*:\s*"(https?:[^"]+)"',
            text,
            re.S,
        ):
            title = unescape(m.group(1)).replace("\\u0026", "&")
            url = unescape(m.group(2)).replace("\\u0026", "&").replace("\\/", "/")
            if not url.startswith("http") or url in seen or blocked(url) or _is_search_host(url):
                continue
            seen.add(url)
            out.append({"url": url, "title": title, "source": "bing-video"})
        if not out:
            for m in re.finditer(r'"pgurl"\s*:\s*"(https?:[^"]+)"', text):
                url = unescape(m.group(1)).replace("\\u0026", "&")
                if url.startswith("http") and url not in seen and not blocked(url) and not _is_search_host(url):
                    seen.add(url)
                    out.append({"url": url, "title": query, "source": "bing-video"})
        # miniatura mais próxima
        for it in out:
            m = re.search(re.escape(it["url"]) + r'.{0,400}?"turl"\s*:\s*"(https?:[^"]+)"', text, re.S)
            if not m:
                m = re.search(r'"turl"\s*:\s*"(https?:[^"]+)".{0,400}?' + re.escape(it["url"]), text, re.S)
            if m:
                it["thumbnail"] = unescape(m.group(1)).replace("\\u0026", "&")
    except Exception:
        pass
    return out


def search_ytdlp(query: str, prefix: str = "ytsearch30", data: dict | None = None) -> list[dict]:
    from app import _extract_info, _ydl_opts

    target = f"{prefix}:{query}"
    opts = _ydl_opts(
        {"skip_download": True, "noplaylist": False, "extract_flat": "in_playlist", "playlistend": 30},
        data_for_url("https://www.youtube.com/", data),
    )
    try:
        info = _extract_info(target, opts, download=False)
    except Exception:
        return []
    if not info:
        return []
    entries = [e for e in (info.get("entries") or []) if e]
    out: list[dict] = []
    for e in entries:
        vid = e.get("id") or ""
        url = e.get("webpage_url") or e.get("url") or ""
        if url and not str(url).startswith("http"):
            if re.fullmatch(r"[\w-]{11}", str(url)) or re.fullmatch(r"[\w-]{11}", str(vid)):
                url = f"https://www.youtube.com/watch?v={url if re.fullmatch(r'[\w-]{11}', str(url)) else vid}"
            else:
                continue
        if not url and vid and re.fullmatch(r"[\w-]{11}", str(vid)):
            url = f"https://www.youtube.com/watch?v={vid}"
        if not url or blocked(url):
            continue
        title = e.get("title") or query
        thumb = None
        thumbs = e.get("thumbnails") or []
        if thumbs:
            thumb = thumbs[-1].get("url") or thumbs[0].get("url")
        thumb = thumb or e.get("thumbnail")
        if not thumb and re.search(r"v=([\w-]{11})", url):
            thumb = f"https://i.ytimg.com/vi/{re.search(r'v=([\w-]{11})', url).group(1)}/hqdefault.jpg"
        out.append({
            "url": url,
            "title": title,
            "thumbnail": thumb,
            "duration": e.get("duration"),
            "source": "ytsearch",
        })
    return out


def _max_format(info: dict) -> tuple[int, int, int, str | None]:
    """Retorna (height, width, filesize_approx, preview_url)."""
    fmts = list(info.get("formats") or [])
    height = int(info.get("height") or 0)
    width = int(info.get("width") or 0)
    size = 0
    preview = None
    video_best = 0
    audio_best = 0
    for f in fmts:
        h = int(f.get("height") or 0)
        w = int(f.get("width") or 0)
        if h > height:
            height, width = h, w
        elif h == height and w > width:
            width = w
        sz = int(f.get("filesize") or f.get("filesize_approx") or 0)
        vcodec = (f.get("vcodec") or "none").lower()
        acodec = (f.get("acodec") or "none").lower()
        if vcodec != "none" and acodec == "none":
            video_best = max(video_best, sz)
        elif acodec != "none" and vcodec == "none":
            audio_best = max(audio_best, sz)
        elif sz > size:
            size = sz
        url = f.get("url") or ""
        ext = (f.get("ext") or "").lower()
        # clipe de prévia: um mp4 progressivo pequeno, se houver
        if preview is None and url.startswith("http") and ext in ("mp4", "m4v", "webm") and h and h <= 480 and vcodec != "none":
            preview = url
    if video_best and audio_best:
        size = max(size, video_best + audio_best)
    if preview is None:
        for f in fmts:
            url = f.get("url") or ""
            if url.startswith("http") and (f.get("ext") or "") in ("mp4", "webm") and (f.get("vcodec") or "none") != "none":
                if int(f.get("height") or 0) <= 360 or f.get("protocol") in ("https", "http"):
                    preview = url
                    break
    return height, width, size, preview


def probe(url: str, data: dict | None = None, timeout: int = 18, html_fallback: bool = False) -> dict | None:
    from app import _extract_info, _ydl_opts, _extract_html_media

    if blocked(url):
        return None
    opts = _ydl_opts(
        {"skip_download": True, "noplaylist": True, "socket_timeout": min(8, timeout)},
        data_for_url(url, data),
    )
    info = None
    try:
        info = _extract_info(url, opts, download=False)
    except (DownloadError, Exception):
        info = None
    if info and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        info = entries[0] if entries else None
    if not info:
        if not html_fallback:
            return None
        fb = _extract_html_media(url)
        if not fb:
            return None
        return {
            "url": url,
            "title": fb.get("title") or url,
            "thumbnail": fb.get("thumbnail"),
            "duration": None,
            "height": 0,
            "width": 0,
            "filesize": 0,
            "site": site_name(url),
            "preview": fb.get("url") if (fb.get("ext") in ("mp4", "webm", "m3u8") and (fb.get("url") or "").startswith("http")) else None,
        }
    page = info.get("webpage_url") or url
    if blocked(page):
        return None
    height, width, size, preview = _max_format(info)
    thumb = info.get("thumbnail")
    thumbs = info.get("thumbnails") or []
    if not thumb and thumbs:
        thumb = thumbs[-1].get("url") or thumbs[0].get("url")
    dur = info.get("duration")
    try:
        dur = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    return {
        "url": page,
        "title": info.get("title") or page,
        "thumbnail": thumb,
        "duration": dur,
        "height": height,
        "width": width,
        "filesize": size,
        "site": site_name(page),
        "preview": preview,
        "extractor": info.get("extractor_key") or info.get("extractor"),
    }




# Palavras tão genéricas que o título do vídeo pode não trazê-las (busca ampla, inclusive conteúdo adulto legal).
_GENERIC_QUERIES = {
    "homem", "mulher", "man", "men", "woman", "women", "girl", "girls", "boy", "boys",
    "sexo", "sex", "porn", "porno", "adulto", "adult", "nsfw", "video", "videos", "filme",
    "movie", "movies", "clipe", "clip", "oficial", "official", "pelada", "pelado", "nua",
    "nu", "naked", "nude", "nudes",
}

_STOP = {
    "de", "da", "do", "das", "dos", "e", "em", "a", "o", "as", "os", "um", "uma",
    "the", "and", "of", "for", "com", "pra", "para", "no", "na", "nos", "nas",
    "por", "que", "se", "ao", "la", "el", "los", "las",
}


def _tokens(s: str) -> list[str]:
    return [w for w in normalize_title(s or "").split() if w]


def _significant(words: list[str]) -> list[str]:
    sig = [w for w in words if len(w) >= 3 and w not in _STOP]
    return sig or [w for w in words if w]


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _token_hit(q: str, hay: set[str], blob: str) -> int:
    if not q:
        return 0
    if q in hay or q in blob:
        return 2
    if any(q in h or (len(h) >= 4 and h in q) for h in hay):
        return 2
    if len(q) >= 4 and any(h.startswith(q[:4]) or (len(h) >= 4 and q.startswith(h[:4])) for h in hay):
        return 1
    maxd = 2 if len(q) >= 7 else 1 if len(q) >= 5 else 0
    if maxd and any(abs(len(h) - len(q)) <= maxd and _levenshtein(h, q) <= maxd for h in hay):
        return 1
    return 0


def match_score(query: str, title: str, url: str = "") -> int:
    q = _significant(_tokens(query))
    if not q:
        return 1
    hay = set(_tokens(title) + _tokens((url or "").replace("-", " ").replace("_", " ")))
    blob = normalize_title((title or "") + " " + (url or ""))
    return sum(_token_hit(t, hay, blob) for t in q)


def _should_require_relevance(query: str) -> bool:
    toks = _significant(_tokens(query))
    if len(toks) >= 2:
        return True
    return bool(toks) and toks[0] not in _GENERIC_QUERIES and len(toks[0]) >= 4


def _relevant(title: str, url: str, query: str) -> bool:
    if not query:
        return True
    return match_score(query, title, url) > 0



_VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "vimeo.com", "dailymotion.com",
    "tiktok.com", "instagram.com", "reddit.com", "redd.it", "x.com", "twitter.com",
    "facebook.com", "fb.watch", "twitch.tv", "rumble.com", "bitchute.com", "odysee.com",
    "archive.org", "blender.org", "pornhub.com", "xvideos.com", "xhamster.com", "xnxx.com",
    "redtube.com", "youporn.com", "spankbang.com", "xhwebsite.com",
)


def is_video_url(url: str) -> bool:
    h = _norm_host(url)
    if any(h == d or h.endswith("." + d) for d in _VIDEO_HOSTS):
        return True
    return bool(re.search(r"\.(mp4|webm|m3u8|mkv|mov)(\?|$)", url or "", re.I))


def height_from_title(title: str) -> int:
    """Palpite de resolução pelo título, usado só quando a sonda falha."""
    t = title or ""
    if re.search(r"\b(8k|4320p)\b", t, re.I):
        return 4320
    if re.search(r"\b(4k|2160p|uhd)\b", t, re.I):
        return 2160
    if re.search(r"\b(1440p|qhd)\b", t, re.I) or re.search(r"\b2k\b", t, re.I):
        return 1440
    if re.search(r"\b(1080p|fhd|full\s*hd)\b", t, re.I):
        return 1080
    if re.search(r"\b(720p|hd)\b", t, re.I):
        return 720
    m = re.search(r"\b(480|360|240)p\b", t, re.I)
    if m:
        return int(m.group(1))
    return 0


def candidate_result(it: dict, info: dict | None = None) -> dict:
    """Monta um resultado mesmo se a sonda de resolução falhar."""
    url = (info or {}).get("url") or it.get("url") or ""
    title = (info or {}).get("title") or it.get("title") or url
    thumb = (info or {}).get("thumbnail") or it.get("thumbnail")
    dur = (info or {}).get("duration")
    if dur is None:
        dur = it.get("duration")
    height = int((info or {}).get("height") or 0)
    if height <= 0:
        height = int(it.get("height") or 0) or height_from_title(title)
    return {
        "url": url,
        "title": title,
        "thumbnail": thumb,
        "duration": dur,
        "height": height,
        "width": int((info or {}).get("width") or 0),
        "filesize": int((info or {}).get("filesize") or 0),
        "site": (info or {}).get("site") or site_name(url),
        "preview": (info or {}).get("preview"),
        "extractor": (info or {}).get("extractor"),
    }


def _accept_candidate(it: dict) -> bool:
    u = it.get("url") or ""
    if not u or blocked(u):
        return False
    if re.search(r"\.(pdf|docx?|zip|rar|exe|apk|css|js)(\?|$)", u, re.I):
        return False
    src = it.get("source") or ""
    if src in ("ytsearch", "bing-video", "pornhub", "xvideos", "xhamster", "xnxx", "site"):
        return True
    return is_video_url(u)


def _site_host(site: str) -> str:
    s = (site or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    h = (urlparse(s).hostname or "").lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def _thumb_near(html: str, pos: int, patterns: tuple[str, ...]) -> str | None:
    chunk = html[max(0, pos - 800): pos + 800]
    for pat in patterns:
        m = re.search(pat, chunk, re.I)
        if m:
            return unescape(m.group(1))
    return None


def search_pornhub(query: str, page: int = 1) -> list[dict]:
    s = _session()
    params: dict[str, str | int] = {"search": query}
    if page > 1:
        params["page"] = page
    html = ""
    for base in ("https://www.pornhub.com/video/search", "https://pt.pornhub.com/video/search"):
        try:
            r = s.get(base, params=params, timeout=6)
            if r.status_code == 200 and "viewkey=" in (r.text or ""):
                html = unescape(r.text or "")
                break
        except Exception:
            continue
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="(/view_video\.php\?viewkey=[a-zA-Z0-9]+)"[^>]*title="([^"]+)"', html):
        url = urljoin("https://www.pornhub.com", m.group(1))
        if url in seen:
            continue
        seen.add(url)
        title = unescape(m.group(2))
        thumb = _thumb_near(html, m.start(), (r'data-mediumthumb="([^"]+)"', r'data-thumb_url="([^"]+)"', r'src="(https://[^"]*phncdn[^"]+)"'))
        out.append({"url": url, "title": title, "thumbnail": thumb, "duration": None, "source": "pornhub", "height": height_from_title(title)})
    return out


def search_xvideos(query: str, page: int = 1) -> list[dict]:
    s = _session()
    params: dict[str, str | int] = {"k": query}
    if page > 1:
        params["p"] = page - 1
    try:
        r = s.get("https://www.xvideos.com/", params=params, timeout=6)
        html = unescape(r.text or "")
    except Exception:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a[^>]+href="(/video[./][^"]+)"[^>]*title="([^"]+)"', html):
        href = m.group(1).split("#")[0]
        url = urljoin("https://www.xvideos.com", href)
        if url in seen:
            continue
        seen.add(url)
        title = unescape(m.group(2))
        thumb = _thumb_near(html, m.start(), (r'data-src="(https://[^"]+)"', r'src="(https://[^"]*xvideos-cdn[^"]+)"'))
        out.append({"url": url, "title": title, "thumbnail": thumb, "duration": None, "source": "xvideos", "height": height_from_title(title)})
    return out


def search_xnxx(query: str, page: int = 1) -> list[dict]:
    s = _session()
    path = "https://www.xnxx.com/search/" + quote_plus(query)
    if page > 1:
        path = path + "/" + str(page)
    try:
        r = s.get(path, timeout=6)
        html = unescape(r.text or "")
    except Exception:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a[^>]+href="(/video-[^"]+)"[^>]*title="([^"]+)"', html):
        url = urljoin("https://www.xnxx.com", m.group(1).split("#")[0])
        if url in seen:
            continue
        seen.add(url)
        title = unescape(m.group(2))
        thumb = _thumb_near(html, m.start(), (r'data-src="(https://[^"]+)"', r'src="(https://[^"]*xnxx-cdn[^"]+)"'))
        out.append({"url": url, "title": title, "thumbnail": thumb, "duration": None, "source": "xnxx", "height": height_from_title(title)})
    return out


def search_xhamster(query: str, page: int = 1) -> list[dict]:
    s = _session()
    url = "https://xhamster.com/search/" + quote_plus(query)
    if page > 1:
        url += f"?page={page}"
    try:
        r = s.get(url, timeout=6)
        html = unescape(r.text or "")
    except Exception:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'"pageURL":"(https:\\/\\/xhamster\.com\\/videos\\/[^"]+)"', html):
        page_url = m.group(1).replace("\\/", "/")
        if page_url in seen:
            continue
        seen.add(page_url)
        chunk = html[max(0, m.start() - 600): m.start() + 400]
        tm = list(re.finditer(r'"title":"((?:\\.|[^"\\])*)"', chunk))
        title = ""
        if tm:
            raw_t = tm[-1].group(1)
            try:
                title = json.loads('"' + raw_t + '"')
            except Exception:
                title = raw_t.replace("\\/", "/").replace('\\"', '"')
        if not title:
            title = page_url.rsplit("/", 1)[-1].replace("-", " ")
        dur = None
        dm = re.search(r'"duration":(\d+)', chunk)
        if dm:
            dur = int(dm.group(1))
        thumb = None
        um = re.search(r'"thumbURL":"(https:\\/\\/[^"]+)"', chunk)
        if um:
            thumb = um.group(1).replace("\\/", "/")
        out.append({"url": page_url, "title": title, "thumbnail": thumb, "duration": dur, "source": "xhamster", "height": height_from_title(title)})
    if not out:
        for href, title in re.findall(r'href="(https://xhamster\.com/videos/[^"?]+)"[^>]*(?:title|alt)="([^"]{3,200})"', html):
            if href in seen:
                continue
            seen.add(href)
            out.append({"url": href, "title": unescape(title), "thumbnail": None, "duration": None, "source": "xhamster", "height": height_from_title(title)})
    return out


def _tube_for_host(host: str, query: str, page: int):
    h = host or ""
    if h == "pornhub.com" or h.endswith(".pornhub.com"):
        return ("ph", lambda: search_pornhub(query, page))
    if h == "xvideos.com" or h.endswith(".xvideos.com"):
        return ("xv", lambda: search_xvideos(query, page))
    if h == "xnxx.com" or h.endswith(".xnxx.com"):
        return ("xnxx", lambda: search_xnxx(query, page))
    if "xhamster" in h:
        return ("xh", lambda: search_xhamster(query, page))
    return None


def _source_jobs(query: str, site: str, data: dict | None, page: int) -> list[tuple[str, Any]]:
    jobs: list[tuple[str, Any]] = []
    host = _site_host(site)
    offset = max(0, (page - 1) * 10)
    if host:
        # Com site= a busca fica restrita àquele domínio; a busca geral é outro pedido, sem site.
        jobs.append(("site_bingv", lambda: search_bing_videos(f"site:{host} {query}", 0 if page == 1 else offset)))
        jobs.append(("site_bing", lambda: search_bing(f"site:{host} {query}", offset)))
        jobs.append(("site_ddg", lambda: search_duckduckgo(f"site:{host} {query}", offset)))
        tube = _tube_for_host(host, query, page)
        if tube:
            jobs.append(tube)
        return jobs
    jobs.extend([
        ("bing", lambda: search_bing_videos(query, offset)),
        ("ddg", lambda: search_duckduckgo(query, offset)),
        ("ph", lambda: search_pornhub(query, page)),
        ("xv", lambda: search_xvideos(query, page)),
        ("xh", lambda: search_xhamster(query, page)),
        ("xnxx", lambda: search_xnxx(query, page)),
        ("yt", lambda: search_ytdlp(query, "ytsearch30" if page == 1 else "ytsearch15", data)),
    ])
    if page == 1:
        q4 = query if re.search(r"\b(4k|2160p|1080p|uhd)\b", query, re.I) else f"{query} 4k"
        jobs.append(("bing4k", lambda: search_bing_videos(q4, 0)))
    return jobs


def _on_site(url: str, host: str) -> bool:
    if not host:
        return True
    h = _norm_host(url)
    return h == host or h.endswith("." + host)


def _ingest(pack: dict[str, Any], items: list[dict]) -> int:
    added = 0
    host = pack.get("site") or ""
    with _CACHE_LOCK:
        seen = pack["seen"]
        for it in items or []:
            if not _accept_candidate(it):
                continue
            u = (it.get("url") or "").strip()
            if not u or u in seen:
                continue
            if host and not _on_site(u, host):
                continue
            seen.add(u)
            pack["pending"].append(it)
            added += 1
        if added:
            pack["event"].set()
    return added


def _source_worker(pack: dict[str, Any], fn) -> None:
    try:
        _ingest(pack, fn() or [])
    except Exception:
        pass
    finally:
        with _CACHE_LOCK:
            pack["inflight"] = max(0, int(pack.get("inflight") or 1) - 1)
            if pack["inflight"] <= 0:
                pack["finished"] = True
            pack["event"].set()


def _start_jobs(pack: dict[str, Any], query: str, site: str, data: dict | None, page: int) -> None:
    jobs = _source_jobs(query, site, data, page)
    with _CACHE_LOCK:
        pack["finished"] = False
        pack["inflight"] = int(pack.get("inflight") or 0) + len(jobs)
    for _name, fn in jobs:
        threading.Thread(target=_source_worker, args=(pack, fn), daemon=True).start()


def _wait_until(pack: dict[str, Any], timeout: float, min_count: int) -> None:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        with _CACHE_LOCK:
            n = len(pack.get("pending") or []) + len(pack.get("done") or [])
            fin = bool(pack.get("finished"))
        if n >= min_count or fin:
            return
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        pack["event"].wait(timeout=min(0.2, remaining))
        pack["event"].clear()


def wait_first(pack: dict[str, Any], timeout: float = 2.5, extra: float = 3.5) -> None:
    """Devolve assim que chegar um lote, sem esperar a fonte mais lenta."""
    _wait_until(pack, timeout, min_count=4)
    with _CACHE_LOCK:
        n = len(pack.get("pending") or []) + len(pack.get("done") or [])
        fin = bool(pack.get("finished"))
    if n or fin:
        return
    _wait_until(pack, extra, min_count=1)


def _cache_key(query: str, url: str, site: str = "") -> str:
    return (query or "").strip().lower() + "||" + (url or "").strip() + "||" + (site or "").strip().lower()


def _new_pack(query: str, source: dict | None, site: str) -> dict[str, Any]:
    return {
        "ts": time.time(),
        "pending": [],
        "done": [],
        "seen": set(),
        "source": source,
        "query": query,
        "site": site,
        "event": threading.Event(),
        "inflight": 0,
        "finished": False,
        "served": False,
        "pages_started": set(),
    }


def gather_candidates(query: str, page: int, data: dict | None = None, site: str = "") -> list[dict]:
    """Junta fontes em paralelo (usado em páginas extras se o lote ao vivo já terminou)."""
    found: list[dict] = []
    seen: set[str] = set()

    def add(items: list[dict]) -> None:
        for it in items:
            if not _accept_candidate(it):
                continue
            u = it.get("url") or ""
            if not u or u in seen:
                continue
            seen.add(u)
            found.append(it)

    jobs = _source_jobs(query, site, data, page)
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as ex:
        futs = [ex.submit(fn) for _name, fn in jobs]
        for fut in as_completed(futs):
            try:
                add(fut.result())
            except Exception:
                pass
    return found


def run_search(query: str, url: str = "", page: int = 1, data: dict | None = None, site: str = "") -> dict:
    """Inicia todas as fontes em paralelo e devolve o primeiro lote (~1–3 s).

    Resolução vem do título na hora; a sonda pesada fica de fora do primeiro resposta.
    Pedidos seguintes (mesmo query) leem o cache e os resultados que ainda estavam chegando.
    """
    query = (query or "").strip()
    url = (url or "").strip()
    site = _site_host(site or (data or {}).get("site") or "")
    page = max(1, int(page or 1))
    if not query and not url:
        return {"ok": False, "error": "Digite o que você quer buscar, ou cole um link."}
    if url and blocked(url):
        return {"ok": False, "error": "Esse site não é permitido (vazamento ou pirataria)."}

    key = _cache_key(query, url, site)
    now = time.time()
    with _CACHE_LOCK:
        pack = _CACHE.get(key)
        if pack and now - pack["ts"] > _CACHE_TTL:
            _CACHE.pop(key, None)
            pack = None

    source = None
    if url and page == 1 and (pack is None or not pack.get("source")) and not query:
        # Só bloqueia na sonda se não houver texto para buscar: o usuário colou só o link.
        source = probe(url, data=data, timeout=22)
        if source:
            query = source.get("title") or query
        if not query:
            return {"ok": False, "error": "Não consegui ler esse link para buscar o mesmo vídeo em outra qualidade."}
        key = _cache_key(query, url, site)

    if not query:
        return {"ok": False, "error": "Digite o que você quer buscar."}

    if pack is None:
        pack = _new_pack(query, source, site)
        with _CACHE_LOCK:
            _CACHE[key] = pack
        pack["pages_started"].add(page)
        _start_jobs(pack, query, site, data, page)
        if url and not pack.get("source"):
            def _bg_probe(pack_ref: dict[str, Any] = pack) -> None:
                try:
                    info = probe(url, data=data, timeout=22)
                    if info:
                        with _CACHE_LOCK:
                            pack_ref["source"] = info
                except Exception:
                    pass
            threading.Thread(target=_bg_probe, daemon=True).start()
        wait_first(pack, timeout=2.5, extra=3.5)
    else:
        if page not in pack.get("pages_started", set()) and (pack.get("finished") or page == 1):
            pack["pages_started"].add(page)
            _start_jobs(pack, pack.get("query") or query, site, data, page)
        if pack.get("served"):
            wait_first(pack, timeout=0.9, extra=0.0)
        else:
            wait_first(pack, timeout=2.5, extra=3.5)

    batch: list[dict] = []
    with _CACHE_LOCK:
        pending = pack["pending"]
        while pending:
            it = pending.pop(0)
            u = it.get("url") or ""
            if not u or blocked(u):
                continue
            src = pack.get("source") or source
            if src and u.rstrip("/") == (src.get("url") or url).rstrip("/"):
                continue
            batch.append(it)

    hits: list[dict] = []
    src = None
    with _CACHE_LOCK:
        src = pack.get("source") or source
    for it in batch:
        if src and src.get("title"):
            if not same_video(src["title"], src.get("duration"), it.get("title") or "", it.get("duration"), min_sim=0.35):
                if title_similarity(src["title"], it.get("title") or "") < 0.35:
                    continue
        hits.append(candidate_result(it))

    with _CACHE_LOCK:
        have = {r.get("url") for r in pack["done"]}
        for h in hits:
            if h.get("url") and h["url"] not in have:
                pack["done"].append(h)
                have.add(h["url"])
        pack["ts"] = time.time()
        pack["served"] = True
        all_hits = list(pack["done"])
        pending_flag = (not pack.get("finished")) or int(pack.get("inflight") or 0) > 0 or bool(pack.get("pending"))
        has_more = pending_flag or page < 8
        src = pack.get("source") or source
        qref = pack.get("query") or query

    all_hits.sort(key=lambda r: (
        1 if is_video_url(r.get("url") or "") else 0,
        match_score(qref, r.get("title") or "", r.get("url") or ""),
        int(r.get("height") or 0),
        float(r.get("duration") or 0),
    ), reverse=True)
    if not all_hits and pending_flag:
        return {
            "ok": True,
            "query": query,
            "page": page,
            "has_more": True,
            "pending": True,
            "source": src,
            "results": [],
        }
    if not all_hits:
        return {"ok": False, "error": "Não achei vídeos com essa busca agora. Tente de novo em instantes.", "query": query, "page": page, "has_more": False, "pending": False, "results": []}
    return {
        "ok": True,
        "query": query,
        "page": page,
        "has_more": has_more,
        "pending": pending_flag,
        "source": src,
        "results": all_hits,
    }


def label_height(h: int) -> str:
    if h >= 2160:
        return "4K"
    if h >= 1440:
        return "1440p"
    if h >= 1080:
        return "1080p"
    if h >= 720:
        return "720p"
    if h >= 480:
        return "480p"
    if h > 0:
        return f"{h}p"
    return "—"
