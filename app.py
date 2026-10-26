#!/usr/bin/env python3
"""Baixar vídeo por link — frontend + API yt-dlp."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file, send_from_directory
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__, static_folder="static", static_url_path="")

MAX_URL_LEN = 2048
TMP_ROOT = Path(tempfile.gettempdir()) / "baixar-video"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

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

    opts = {
        **YDL_BASE,
        "skip_download": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        msg = str(e).split("\n")[-1][:300]
        return jsonify({"ok": False, "error": f"Não consegui ler esse link. {msg}"}), 422
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Erro: {e}"}), 500

    if not info:
        return jsonify({"ok": False, "error": "Nada encontrado nesse link."}), 422

    # playlist guard
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if entries:
            info = entries[0]
        else:
            return jsonify({"ok": False, "error": "Isso parece uma lista, não um vídeo só."}), 422

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


@app.post("/api/download")
def api_download():
    """Resolve o melhor arquivo e devolve link direto (redirect) ou o arquivo."""
    data = request.get_json(silent=True) or {}
    try:
        url = _clean_url(data.get("url", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    mode = (data.get("mode") or "video").strip().lower()
    if mode not in ("video", "audio"):
        mode = "video"

    # 1) tentar URL direta (rápido, não passa pelo servidor)
    if mode == "video":
        # prefer progressive single file; fall back to any best
        fmt = "b[ext=mp4]/b[ext=webm]/b/best"
    else:
        fmt = "bestaudio/best"

    opts_g = {
        **YDL_BASE,
        "format": fmt,
    }
    try:
        with YoutubeDL(opts_g) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and info.get("_type") == "playlist":
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    return jsonify({"ok": False, "error": "Lista vazia."}), 422
                info = entries[0]
            # requested downloads may be multiple (video+audio)
            req = info.get("requested_formats") or []
            if len(req) <= 1:
                direct = info.get("url")
                if direct and not info.get("is_live"):
                    # some URLs need headers; prefer proxy download then
                    http_headers = info.get("http_headers") or {}
                    # If cookies/headers critical, fall through to server download
                    needs_headers = bool(http_headers.get("Authorization") or http_headers.get("Cookie"))
                    if not needs_headers and direct.startswith("http"):
                        return jsonify(
                            {
                                "ok": True,
                                "method": "redirect",
                                "url": direct,
                                "filename": _safe_name(info.get("title"), info.get("ext") or "mp4"),
                                "title": info.get("title") or "Vídeo",
                            }
                        )
    except DownloadError as e:
        msg = str(e).split("\n")[-1][:300]
        return jsonify({"ok": False, "error": f"Não consegui baixar. {msg}"}), 422
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Erro: {e}"}), 500

    # 2) baixar no servidor (merge se preciso)
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
            # audio postprocess changes ext
            if mode == "audio":
                cand = path.with_suffix(".mp3")
                if cand.exists():
                    path = cand
            if not path.exists():
                # try common alts
                for p in TMP_ROOT.glob(f"{info.get('id')}.*"):
                    path = p
                    break
            if not path.exists():
                return jsonify({"ok": False, "error": "Download terminou sem arquivo."}), 500
    except DownloadError as e:
        msg = str(e).split("\n")[-1][:300]
        return jsonify({"ok": False, "error": f"Não consegui baixar. {msg}"}), 422
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
