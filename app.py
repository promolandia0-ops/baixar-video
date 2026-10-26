#!/usr/bin/env python3
"""Baixar vídeo por link — yt-dlp + fallback HTML (KVS / .mp4 embutido)."""
from __future__ import annotations

import os
import re
import tempfile
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__, static_folder="static", static_url_path="")

MAX_URL_LEN = 2048
TMP_ROOT = Path(tempfile.gettempdir()) / "baixar-video"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

YDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "noplaylist": True,
    "retries": 3,
    "socket_timeout": 30,
}


def _clean_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u:
        raise ValueError("Cole o link do vídeo.")
    if len(u) > MAX_URL_LEN:
        raise ValueError("Link muito longo.")
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Link inválido.")
    return u


def _safe_name(title: str | None, ext: str) -> str:
    base = re.sub(r"[^\w\-. ]+", "", (title or "video"), flags=re.UNICODE).strip() or "video"
    base = base[:80]
    ext = (ext or "mp4").lstrip(".")
    return f"{base}.{ext}"


def _page_title(html: str, fallback: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return fallback
    title = re.sub(r"\s+", " ", unescape(m.group(1))).strip()
    title = re.split(r"\s*[|\-–—]\s*", title)[0].strip() or fallback
    return title[:120]


def _score_media(u: str) -> int:
    low = u.lower()
    score = 0
    if "720p" in low or "_720" in low:
        score += 30
    if "1080" in low:
        score += 40
    if "480p" in low or "_480" in low:
        score += 10
    if "preview" in low or "trailer" in low or ".jpg" in low:
        score -= 100
    if "/get_file/" in low:
        score += 20
    if "v-acctoken" in low or "acctoken" in low:
        score += 50  # KVS hotlink token — required to avoid 403
    if low.endswith(".m3u8") or ".m3u8?" in low or ".m3u8/" in low:
        score += 5
    if ".mp4" in low:
        score += 15
    return score



def _parse_media_from_html(page_url: str, html: str) -> dict | None:
    """Parse direct mp4/m3u8 / KVS video_url from HTML."""
    candidates: list[str] = []

    for m in re.finditer(
        r"""video_(?:alt_)?url\s*:\s*['"]([^'"]+)['"]""",
        html,
        re.I,
    ):
        candidates.append(unescape(m.group(1)))

    for m in re.finditer(
        r"""<(?:source|video)[^>]+src=["']([^"']+)["']""",
        html,
        re.I,
    ):
        candidates.append(unescape(m.group(1)))

    for m in re.finditer(
        r"""https?://[^\s"'<>]+?\.(?:mp4|m3u8|webm)/?(?:\?[^\s"'<>]*)?""",
        html,
        re.I,
    ):
        candidates.append(unescape(m.group(0)))

    for m in re.finditer(
        r"""["'](/[^"']*?get_file/[^"']+\.mp4[^"']*)["']""",
        html,
        re.I,
    ):
        candidates.append(unescape(m.group(1)))

    for m in re.finditer(
        r"""['"](/(?:get_file|contents)[^'"]+\.mp4[^'"]*)['"]""",
        html,
        re.I,
    ):
        candidates.append(unescape(m.group(1)))

    abs_urls: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        c = c.strip()
        if not c or c.startswith("data:"):
            continue
        full = urljoin(page_url, c)
        if full in seen:
            continue
        seen.add(full)
        if not re.search(r"\.(mp4|m3u8|webm)(/|\?|$)", full, re.I):
            continue
        abs_urls.append(full)

    if not abs_urls:
        return None

    abs_urls.sort(key=_score_media, reverse=True)
    best = abs_urls[0]
    if _score_media(best) < 0:
        return None

    title = _page_title(html, urlparse(page_url).path.rstrip("/").split("/")[-1] or "video")
    thumb = None
    tm = re.search(
        r"""(?:og:image|twitter:image)["']\s+content=["']([^"']+)["']""",
        html,
        re.I,
    ) or re.search(r"""content=["']([^"']+)["'][^>]+(?:og:image|twitter:image)""", html, re.I)
    if tm:
        thumb = urljoin(page_url, unescape(tm.group(1)))

    return {
        "title": title,
        "url": best,
        "thumbnail": thumb,
        "extractor": "html-fallback",
        "webpage_url": page_url,
        "ext": "mp4" if ".mp4" in best.lower() else ("m3u8" if ".m3u8" in best.lower() else "webm"),
        "candidates": abs_urls[:5],
    }


def _fetch_page(page_url: str, session: requests.Session | None = None) -> tuple[requests.Session, str]:
    s = session or requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    r = s.get(page_url, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return s, r.text


def _extract_html_media(page_url: str, session: requests.Session | None = None) -> dict | None:
    """Fallback when yt-dlp fails: scrape direct mp4/m3u8 / KVS video_url."""
    try:
        _s, html = _fetch_page(page_url, session)
    except requests.RequestException:
        return None
    return _parse_media_from_html(page_url, html)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/info")
def api_info():
    data = request.get_json(silent=True) or {}
    try:
        url = _clean_url(data.get("url", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    opts = {**YDL_BASE, "skip_download": True}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                return jsonify({"ok": False, "error": "Isso parece uma lista, não um vídeo só."}), 422
            info = entries[0]
        return jsonify(
            {
                "ok": True,
                "title": info.get("title") or "Vídeo",
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "extractor": info.get("extractor_key") or info.get("extractor"),
                "webpage_url": info.get("webpage_url") or url,
            }
        )
    except DownloadError as e:
        fb = _extract_html_media(url)
        if fb:
            return jsonify(
                {
                    "ok": True,
                    "title": fb["title"],
                    "duration": None,
                    "thumbnail": fb.get("thumbnail"),
                    "extractor": fb["extractor"],
                    "webpage_url": url,
                }
            )
        msg = str(e).split("\n")[-1][:300]
        return jsonify({"ok": False, "error": f"Não consegui ler esse link. {msg}"}), 422
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Erro: {e}"}), 500


@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    try:
        url = _clean_url(data.get("url", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    mode = (data.get("mode") or "video").strip().lower()
    if mode not in ("video", "audio"):
        mode = "video"

    if mode == "video":
        fmt = "b[ext=mp4]/b[ext=webm]/b/best"
    else:
        fmt = "bestaudio/best"

    # 1) yt-dlp direct URL
    try:
        with YoutubeDL({**YDL_BASE, "format": fmt}) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and info.get("_type") == "playlist":
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    return jsonify({"ok": False, "error": "Lista vazia."}), 422
                info = entries[0]
            req = info.get("requested_formats") or []
            if len(req) <= 1:
                direct = info.get("url")
                http_headers = info.get("http_headers") or {}
                needs_headers = bool(http_headers.get("Authorization") or http_headers.get("Cookie"))
                if direct and not info.get("is_live") and not needs_headers and direct.startswith("http"):
                    return jsonify(
                        {
                            "ok": True,
                            "method": "redirect",
                            "url": direct,
                            "filename": _safe_name(info.get("title"), info.get("ext") or "mp4"),
                            "title": info.get("title") or "Vídeo",
                        }
                    )
    except DownloadError:
        # fall through to HTML / server download
        pass
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Erro: {e}"}), 500

    # 2) HTML fallback (KVS / embedded mp4) — video mode only
    # Many tube sites bind get_file tokens to IP/cookies, so we PROXY through this server
    # instead of giving the user a direct URL (which returns 403 on their phone).
    if mode == "video":
        fb = _extract_html_media(url)
        if fb and fb.get("url"):
            filename = _safe_name(fb.get("title"), fb.get("ext") or "mp4")
            return jsonify(
                {
                    "ok": True,
                    "method": "proxy",
                    "url": "/api/proxy",
                    "page_url": url,
                    "filename": filename,
                    "title": fb.get("title") or "Vídeo",
                }
            )

    # 3) server-side download/merge
    outtmpl = str(TMP_ROOT / "%(id)s.%(ext)s")
    opts_dl = {
        **YDL_BASE,
        "format": fmt if mode == "audio" else "bv*+ba/b/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "restrictfilenames": True,
    }
    if mode == "audio":
        opts_dl["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    try:
        with YoutubeDL(opts_dl) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get("_type") == "playlist":
                entries = [e for e in (info.get("entries") or []) if e]
                info = entries[0] if entries else info
            path = Path(ydl.prepare_filename(info))
            if mode == "audio":
                cand = path.with_suffix(".mp3")
                if cand.exists():
                    path = cand
            if not path.exists():
                for p in TMP_ROOT.glob(f"{info.get('id')}.*"):
                    path = p
                    break
            if not path.exists():
                return jsonify({"ok": False, "error": "Download terminou sem arquivo."}), 500
    except DownloadError as e:
        msg = str(e).split("\n")[-1][:300]
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Não consegui baixar esse link. "
                    "Alguns sites protegem o vídeo (login, DRM ou player novo). "
                    f"{msg}"
                ),
            }
        ), 422
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Erro no download: {e}"}), 500

    filename = _safe_name(info.get("title"), path.suffix.lstrip(".") or "mp4")
    return send_file(
        path,
        as_attachment=True,
        download_name=filename,
        mimetype="application/octet-stream",
        max_age=0,
    )


@app.post("/api/proxy")
def api_proxy():
    """Stream media using the SAME session/cookies that opened the page (avoids 403)."""
    data = request.get_json(silent=True) or {}
    try:
        page_url = _clean_url(data.get("page_url") or data.get("url") or "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    try:
        session, html = _fetch_page(page_url)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"Não abri a página: {e}"}), 502

    fb = _parse_media_from_html(page_url, html)
    if not fb or not fb.get("url"):
        return jsonify({"ok": False, "error": "Não achei o arquivo de vídeo nessa página."}), 422

    media_url = fb["url"]
    filename = _safe_name(fb.get("title"), fb.get("ext") or "mp4")
    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"

    try:
        r = session.get(
            media_url,
            headers={
                "Referer": page_url,
                "Origin": origin,
                "Accept": "*/*",
            },
            stream=True,
            timeout=120,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"Falha ao buscar o vídeo: {e}"}), 502

    if r.status_code >= 400:
        body = ""
        try:
            body = r.text[:120]
        except Exception:
            pass
        r.close()
        return jsonify(
            {
                "ok": False,
                "error": f"O site de origem recusou o download (HTTP {r.status_code}). {body}".strip(),
            }
        ), 502

    content_type = r.headers.get("Content-Type") or "video/mp4"

    def generate():
        try:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            r.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }
    cl = r.headers.get("Content-Length")
    if cl:
        headers["Content-Length"] = cl

    return Response(generate(), headers=headers, mimetype=content_type)



if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
