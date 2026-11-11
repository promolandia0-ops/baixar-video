#!/usr/bin/env python3
"""Busca ampla por vídeos em melhor qualidade (4K/1080p), sem filtro de idade.

Safe search desligado (o usuário é adulto; conteúdo adulto legal é permitido).
Sites de vazamento e pirataria entram na blocklist e são descartados.
"""
from __future__ import annotations

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
    "movie", "movies", "clipe", "clip", "oficial", "official",
}


def _should_require_relevance(query: str) -> bool:
    toks = [w for w in normalize_title(query).split() if len(w) >= 3]
    if len(toks) >= 2:
        return True
    return bool(toks) and toks[0] not in _GENERIC_QUERIES and len(toks[0]) >= 4


def _relevant(title: str, url: str, query: str) -> bool:
    toks = [w for w in normalize_title(query).split() if len(w) >= 3]
    if not toks:
        return True
    blob = normalize_title((title or "") + " " + (url or ""))
    hit = sum(1 for w in toks if w in blob)
    need = 1 if len(toks) == 1 else min(2, len(toks))
    return hit >= need


def gather_candidates(query: str, page: int, data: dict | None = None) -> list[dict]:
    """Junta DDG + Bing + ytsearch, sem filtro de idade, com blocklist."""
    offset = max(0, (page - 1) * 10)
    q4 = query if re.search(r"\b(4k|2160p|1080p|uhd)\b", query, re.I) else f"{query} 4k"
    found: list[dict] = []
    seen: set[str] = set()

    def add(items: list[dict]) -> None:
        for it in items:
            u = it.get("url") or ""
            if not u or blocked(u) or u in seen:
                continue
            # ignora arquivos que claramente não são página/vídeo
            if re.search(r"\.(pdf|docx?|zip|rar|exe|apk|css|js)(\?|$)", u, re.I):
                continue
            src = it.get("source") or ""
            if src not in ("ytsearch", "bing-video") and not is_video_url(u):
                continue
            if query and _should_require_relevance(query) and not _relevant(it.get("title") or "", u, query):
                continue
            seen.add(u)
            found.append(it)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [
            ex.submit(search_duckduckgo, query, offset),
            ex.submit(search_bing, query, offset),
            ex.submit(search_bing_videos, query, offset),
            ex.submit(search_ytdlp, query, "ytsearch30", data),
        ]
        if page == 1:
            futs.append(ex.submit(search_duckduckgo, q4, 0))
            futs.append(ex.submit(search_bing_videos, q4, 0))
            futs.append(ex.submit(search_ytdlp, q4, "ytsearch15", data))
        for fut in as_completed(futs):
            try:
                add(fut.result())
            except Exception:
                pass
    return found




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
        height = height_from_title(title)
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


def _cache_key(query: str, url: str) -> str:
    return (query or "").strip().lower() + "||" + (url or "").strip()


def run_search(query: str, url: str = "", page: int = 1, data: dict | None = None) -> dict:
    """Página 1: metadados do link (se houver) + busca + sonda de resolução.
    Páginas seguintes: mais candidatos do cache / nova rodada de busca.
    """
    query = (query or "").strip()
    url = (url or "").strip()
    page = max(1, int(page or 1))
    if not query and not url:
        return {"ok": False, "error": "Digite o que você quer buscar, ou cole um link."}
    if url and blocked(url):
        return {"ok": False, "error": "Esse site não é permitido (vazamento ou pirataria)."}

    key = _cache_key(query, url)
    now = time.time()
    with _CACHE_LOCK:
        pack = _CACHE.get(key)
        if pack and now - pack["ts"] > _CACHE_TTL:
            _CACHE.pop(key, None)
            pack = None

    source = None
    if url and page == 1:
        source = probe(url, data=data, timeout=22)
        if source and not query:
            query = source.get("title") or query
        if not query:
            return {"ok": False, "error": "Não consegui ler esse link para buscar o mesmo vídeo em outra qualidade."}

    if not query:
        return {"ok": False, "error": "Digite o que você quer buscar."}

    if pack is None:
        cands = gather_candidates(query, 1, data=data)
        pack = {"ts": now, "pending": cands, "done": [], "seen": set(), "source": source, "query": query}
        with _CACHE_LOCK:
            _CACHE[key] = pack
    elif page == 1 and not pack.get("done"):
        pass
    elif page > 1 and len(pack.get("pending") or []) < 8:
        more = gather_candidates(pack.get("query") or query, page, data=data)
        with _CACHE_LOCK:
            seen = pack["seen"]
            for it in more:
                u = it.get("url")
                if u and u not in seen and not blocked(u):
                    pack["pending"].append(it)

    # Entrega um lote grande de candidatos mesmo se a sonda falhar.
    # Sonda pesada (yt-dlp em paralelo) derrubava o worker no plano free (HTTP 502) e esvaziava a lista.
    batch_size = 40 if page == 1 else 24
    batch: list[dict] = []
    with _CACHE_LOCK:
        pending = pack["pending"]
        while pending and len(batch) < batch_size:
            it = pending.pop(0)
            u = it.get("url") or ""
            if not u or u in pack["seen"] or blocked(u):
                continue
            if source and u.rstrip("/") == (source.get("url") or url).rstrip("/"):
                continue
            pack["seen"].add(u)
            batch.append(it)

    hits: list[dict] = []
    for it in batch:
        if source and source.get("title"):
            if not same_video(source["title"], source.get("duration"), it.get("title") or "", it.get("duration"), min_sim=0.35):
                # sem duração/sonda: ainda assim aceita se o título for parecido o bastante
                if title_similarity(source["title"], it.get("title") or "") < 0.35:
                    continue
        hits.append(candidate_result(it))

    with _CACHE_LOCK:
        pack["done"].extend(hits)
        pack["ts"] = time.time()
        all_hits = list(pack["done"])
        has_more = bool(pack["pending"]) or page < 8
        src = pack.get("source") or source

    qref = pack.get("query") or query
    all_hits.sort(key=lambda r: (
        1 if is_video_url(r.get("url") or "") else 0,
        1 if _relevant(r.get("title") or "", r.get("url") or "", qref) else 0,
        int(r.get("height") or 0),
        float(r.get("duration") or 0),
    ), reverse=True)
    if not all_hits:
        return {"ok": False, "error": "Não achei vídeos com essa busca agora. Tente de novo em instantes.", "query": query, "page": page, "has_more": False, "results": []}
    return {
        "ok": True,
        "query": query,
        "page": page,
        "has_more": has_more,
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
