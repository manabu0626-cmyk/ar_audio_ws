import asyncio
import base64
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx
import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger("ar_audio_admin")
logging.basicConfig(level=logging.INFO)

# ── configuration ──────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_PKG = _HERE.parent  # .../ar_audio/


def _load_env_file(path: str) -> None:
    """Parse KEY=VALUE (and systemd Environment=KEY=VALUE) into os.environ.
    Uses setdefault so already-set vars are never overwritten."""
    try:
        with open(path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip systemd "Environment=" prefix
                if line.startswith("Environment="):
                    line = line[len("Environment="):]
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, val)
    except OSError:
        pass  # file absent or unreadable — not an error


# Try these sources in order (setdefault means first writer wins)
for _env_src in [
    os.environ.get("ENV_FILE", ""),
    "/etc/ar_audio_admin.env",
    "/etc/systemd/system/ar_audio_admin.service",   # also handles inline Environment= lines
    str(Path(__file__).parents[3] / "ar_audio_admin.service"),  # workspace root
]:
    if _env_src:
        _load_env_file(_env_src)

# Paths are resolved once at startup (safe — no secrets here)
POINTS_FILE = Path(os.environ.get(
    "AR_POINTS_FILE",
    str(_PKG / "config" / "ar_points.yaml"),
))
LANGUAGE_FILE = Path(os.environ.get(
    "AR_LANGUAGE_FILE",
    str(_PKG / "config" / "language.yaml"),
))
AUDIO_BASE = Path(os.environ.get(
    "AUDIO_BASE_PATH",
    str(_PKG / "audio"),
))

AUDIO_BASE.mkdir(parents=True, exist_ok=True)


# API keys are read on every request so that env changes take effect without restart.
def _openai_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "")


def _google_tts_key() -> str:
    return os.environ.get("GOOGLE_TTS_KEY", "")

# lang_code → (google_tts_language_code, ssml_gender)
SUPPORTED_LANGUAGES: Dict[str, tuple] = {
    "ja":    ("ja-JP", "NEUTRAL"),
    "en":    ("en-US", "NEUTRAL"),
    "zh-CN": ("zh-CN", "NEUTRAL"),
    "zh-TW": ("zh-TW", "NEUTRAL"),
    "ko":    ("ko-KR", "NEUTRAL"),
    "es":    ("es-ES", "NEUTRAL"),
    "fr":    ("fr-FR", "NEUTRAL"),
    "it":    ("it-IT", "NEUTRAL"),
    "de":    ("de-DE", "NEUTRAL"),
    "pt":    ("pt-PT", "NEUTRAL"),
    "th":    ("th-TH", "NEUTRAL"),
    "vi":    ("vi-VN", "NEUTRAL"),
}

LANG_NAMES: Dict[str, str] = {
    "ja":    "日本語",
    "en":    "English",
    "zh-CN": "中文简体",
    "zh-TW": "中文繁體",
    "ko":    "한국어",
    "es":    "Español",
    "fr":    "Français",
    "it":    "Italiano",
    "de":    "Deutsch",
    "pt":    "Português",
    "th":    "ภาษาไทย",
    "vi":    "Tiếng Việt",
}

# ── models ─────────────────────────────────────────────────────────────────────

class ARPointIn(BaseModel):
    name: str
    latitude: float
    longitude: float
    audio_file: Optional[str] = None
    radius: float = 10.0
    tts_texts: Dict[str, str] = Field(default_factory=dict)
    audio_files: Dict[str, str] = Field(default_factory=dict)


# ── YAML helpers ───────────────────────────────────────────────────────────────

def _load() -> List[dict]:
    if not POINTS_FILE.exists():
        return []
    with open(POINTS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    pts = data.get("ar_points", [])
    dirty = False
    for p in pts:
        if not p.get("id"):
            p["id"] = uuid.uuid4().hex[:8]
            dirty = True
    if dirty:
        _save(pts)
    return pts


def _save(pts: List[dict]) -> None:
    POINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(POINTS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(
            {"ar_points": pts},
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )


def _load_language() -> str:
    if not LANGUAGE_FILE.exists():
        return "ja"
    with open(LANGUAGE_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("language", "ja")


def _save_language(lang: str) -> None:
    LANGUAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LANGUAGE_FILE, "w", encoding="utf-8") as f:
        yaml.dump({"language": lang}, f, allow_unicode=True, default_flow_style=False)


# ── translation & TTS helpers ──────────────────────────────────────────────────

async def _translate_text(japanese_text: str, target_lang: str) -> str:
    if target_lang == "ja":
        return japanese_text

    key = _openai_key()
    logger.info("OPENAI_API_KEY prefix: %s", (key[:10] + "...") if key else "(not set)")
    if not key:
        raise HTTPException(500, "OPENAI_API_KEY is not configured")

    lang_name = LANG_NAMES.get(target_lang, target_lang)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"You are a professional translator specializing in audio guide scripts. "
                            f"Translate the following Japanese text to {lang_name}. "
                            f"Output only the translated text, nothing else."
                        ),
                    },
                    {"role": "user", "content": japanese_text},
                ],
                "temperature": 0.3,
            },
        )
    if r.status_code != 200:
        raise HTTPException(502, f"OpenAI API error: {r.text[:400]}")
    return r.json()["choices"][0]["message"]["content"].strip()


async def _generate_tts(text: str, lang: str, output_path: Path) -> int:
    key = _google_tts_key()
    logger.info("GOOGLE_TTS_KEY prefix: %s", (key[:10] + "...") if key else "(not set)")
    if not key:
        raise HTTPException(500, "GOOGLE_TTS_KEY is not configured")

    lc, gender = SUPPORTED_LANGUAGES[lang]
    url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={key}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json={
            "input": {"text": text},
            "voice": {"languageCode": lc, "ssmlGender": gender},
            "audioConfig": {"audioEncoding": "MP3"},
        })
    if r.status_code != 200:
        raise HTTPException(502, f"Google TTS API error: {r.text[:400]}")

    audio = base64.b64decode(r.json()["audioContent"])
    output_path.write_bytes(audio)
    return len(audio)


def _delete_point_audio_files(pid: str, langs: Optional[List[str]] = None) -> List[str]:
    deleted = []
    target_langs = langs if langs is not None else list(SUPPORTED_LANGUAGES.keys())
    for lang in target_langs:
        fpath = AUDIO_BASE / f"{pid}_{lang}.mp3"
        if fpath.exists():
            fpath.unlink()
            deleted.append(f"{pid}_{lang}.mp3")
    return deleted


# ── WebSocket GNSS broadcast ────────────────────────────────────────────────────

_ws_clients: Set[WebSocket] = set()
_ws_clients_lock = threading.Lock()
_main_loop: Optional[asyncio.AbstractEventLoop] = None


async def _broadcast_gnss(payload: str) -> None:
    dead = []
    with _ws_clients_lock:
        clients = list(_ws_clients)
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    if dead:
        with _ws_clients_lock:
            _ws_clients.difference_update(dead)


def _gnss_relay_thread() -> None:
    loop = _main_loop
    if loop is None:
        return

    cmd = (
        'source /opt/ros/humble/setup.bash && '
        'ros2 topic echo /sensing/gnss/fix sensor_msgs/msg/NavSatFix'
    )
    # PYTHONUNBUFFERED=1: ros2 is a Python script; without this it uses
    # block-buffering when stdout is a pipe (~8 KB delay).
    env = dict(os.environ)
    env['PYTHONUNBUFFERED'] = '1'

    while True:
        proc = None
        try:
            proc = subprocess.Popen(
                ['/bin/bash', '-c', cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
            logger.info('GNSS relay started (PID %d)', proc.pid)

            lat: Optional[float] = None
            lon: Optional[float] = None
            msg_count = 0
            line_count = 0

            # Use readline() explicitly — the for-loop iterator on TextIOWrapper
            # can silently do read-ahead buffering that defeats PYTHONUNBUFFERED.
            while True:
                raw = proc.stdout.readline()
                if not raw:   # subprocess closed stdout (EOF)
                    break
                line = raw.strip()
                line_count += 1

                # Raw line debug: log first 30 lines per subprocess session
                if line_count <= 30:
                    logger.info('GNSS raw[%d]: %r', line_count, line)

                if line.startswith('latitude:'):
                    try:
                        lat = float(line.split(':', 1)[1].strip())
                        logger.info('GNSS lat parsed: %.7f', lat)
                    except ValueError as e:
                        logger.warning('GNSS lat parse error: %r → %s', line, e)

                elif line.startswith('longitude:'):
                    try:
                        lon = float(line.split(':', 1)[1].strip())
                        logger.info('GNSS lon parsed: %.7f', lon)
                    except ValueError as e:
                        logger.warning('GNSS lon parse error: %r → %s', line, e)

                    # Broadcast as soon as longitude arrives (latitude already set).
                    # Don't wait for '---' — longitude is the last value we need.
                    if lat is not None and lon is not None:
                        msg_count += 1
                        n_clients = len(_ws_clients)
                        logger.info(
                            'GNSS broadcast #%d lat=%.6f lon=%.6f clients=%d',
                            msg_count, lat, lon, n_clients,
                        )
                        data = json.dumps({'lat': lat, 'lon': lon})
                        asyncio.run_coroutine_threadsafe(
                            _broadcast_gnss(data), loop
                        )
                        lat = lon = None

                elif line == '---':
                    # Message boundary — reset any partial state
                    lat = lon = None

            stderr_tail = proc.stderr.read(500)
            proc.wait()
            logger.warning(
                'GNSS relay subprocess exited (rc=%d, lines=%d) stderr=%r',
                proc.returncode, line_count, stderr_tail,
            )
        except Exception as exc:
            logger.warning('GNSS relay error: %s', exc)
        finally:
            if proc and proc.poll() is None:
                proc.kill()

        time.sleep(5)


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="AR Audio Admin", version="2.0.0")


@app.on_event('startup')
async def _startup() -> None:
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    t = threading.Thread(target=_gnss_relay_thread, daemon=True)
    t.start()


@app.websocket('/ws/gnss')
async def gnss_websocket(ws: WebSocket) -> None:
    await ws.accept()
    with _ws_clients_lock:
        _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_clients_lock:
            _ws_clients.discard(ws)


@app.get("/", response_class=HTMLResponse)
async def root():
    return (_HERE / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/points")
async def get_points():
    return _load()


@app.post("/api/points", status_code=201)
async def create_point(body: ARPointIn):
    pts = _load()
    d = body.model_dump()
    d["id"] = uuid.uuid4().hex[:8]
    pts.append(d)
    _save(pts)
    return d


@app.put("/api/points/{pid}")
async def update_point(pid: str, body: ARPointIn):
    pts = _load()
    for i, p in enumerate(pts):
        if p.get("id") == pid:
            old_ja_text = (p.get("tts_texts") or {}).get("ja", "")
            new_ja_text = (body.tts_texts or {}).get("ja", "")

            d = body.model_dump()
            d["id"] = pid

            if old_ja_text != new_ja_text:
                # Japanese source text changed → all derived mp3s are stale
                _delete_point_audio_files(pid)
                d["audio_files"] = {}
                d["audio_file"] = None
            else:
                if not d.get("audio_files"):
                    d["audio_files"] = p.get("audio_files", {})
                if d.get("audio_file") is None:
                    d["audio_file"] = p.get("audio_file")

            pts[i] = d
            _save(pts)
            return d
    raise HTTPException(404, "Point not found")


@app.delete("/api/points/{pid}")
async def delete_point(pid: str):
    pts = _load()
    new_pts = [p for p in pts if p.get("id") != pid]
    if len(new_pts) == len(pts):
        raise HTTPException(404, "Point not found")
    _delete_point_audio_files(pid)
    _save(new_pts)
    return {"ok": True}


# ── language management ────────────────────────────────────────────────────────

@app.get("/api/language")
async def get_language():
    return {
        "language": _load_language(),
        "supported": list(SUPPORTED_LANGUAGES.keys()),
        "names": LANG_NAMES,
    }


@app.put("/api/language")
async def set_language(lang: str):
    if lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(400, f"Unsupported language: {lang}")
    _save_language(lang)

    # Update audio_file for every point that already has this language's mp3.
    # This makes the admin UI reflect the active file immediately after switching.
    pts = _load()
    updated = 0
    for p in pts:
        pid = p.get("id", "")
        if not pid:
            continue
        fname = f"{pid}_{lang}.mp3"
        if (AUDIO_BASE / fname).exists():
            p.setdefault("audio_files", {})[lang] = fname
            p["audio_file"] = fname
            updated += 1
    if updated:
        _save(pts)
        logger.info("set_language(%s): updated audio_file for %d points", lang, updated)

    return {"language": lang, "updated_points": updated}


@app.get("/api/lang_status")
async def get_lang_status():
    pts = _load()
    result: Dict[str, Dict[str, bool]] = {}
    for p in pts:
        pid = p.get("id")
        if not pid:
            continue
        result[pid] = {
            lang: (AUDIO_BASE / f"{pid}_{lang}.mp3").exists()
            for lang in SUPPORTED_LANGUAGES
        }
    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})


# ── audio generation ───────────────────────────────────────────────────────────

@app.post("/api/points/{pid}/generate/{lang}")
async def generate_audio(pid: str, lang: str):
    if lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(400, f"Unsupported language: {lang}")

    pts = _load()
    idx = next((i for i, p in enumerate(pts) if p.get("id") == pid), None)
    if idx is None:
        raise HTTPException(404, "Point not found")

    ja_text = ((pts[idx].get("tts_texts") or {}).get("ja", "") or "").strip()
    if not ja_text:
        raise HTTPException(400, "Japanese text (tts_texts.ja) is empty")

    fname = f"{pid}_{lang}.mp3"
    fpath = AUDIO_BASE / fname

    if fpath.exists():
        # still update audio_file pointer so the UI shows the correct active file
        pts[idx].setdefault("audio_files", {})[lang] = fname
        pts[idx]["audio_file"] = fname
        _save(pts)
        return {"filename": fname, "cached": True, "bytes": fpath.stat().st_size}

    translated = await _translate_text(ja_text, lang)
    nbytes = await _generate_tts(translated, lang, fpath)

    pts[idx].setdefault("audio_files", {})[lang] = fname
    pts[idx]["audio_file"] = fname  # always switch active file to the generated language
    _save(pts)

    return {"filename": fname, "cached": False, "bytes": nbytes, "translated_text": translated}


@app.post("/api/points/{pid}/regenerate/{lang}")
async def regenerate_audio(pid: str, lang: str):
    if lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(400, f"Unsupported language: {lang}")

    pts = _load()
    idx = next((i for i, p in enumerate(pts) if p.get("id") == pid), None)
    if idx is None:
        raise HTTPException(404, "Point not found")

    ja_text = ((pts[idx].get("tts_texts") or {}).get("ja", "") or "").strip()
    if not ja_text:
        raise HTTPException(400, "Japanese text is empty")

    fname = f"{pid}_{lang}.mp3"
    fpath = AUDIO_BASE / fname
    if fpath.exists():
        fpath.unlink()

    translated = await _translate_text(ja_text, lang)
    nbytes = await _generate_tts(translated, lang, fpath)

    pts[idx].setdefault("audio_files", {})[lang] = fname
    pts[idx]["audio_file"] = fname  # always switch active file to the generated language
    _save(pts)

    return {"filename": fname, "bytes": nbytes, "translated_text": translated}


# ── cleanup ────────────────────────────────────────────────────────────────────

@app.post("/api/cleanup_orphans")
async def cleanup_orphans():
    pts = _load()
    valid_ids = {p["id"] for p in pts if p.get("id")}
    deleted = []

    for fpath in AUDIO_BASE.glob("*.mp3"):
        # expected format: {pid}_{lang}.mp3 — pid has no underscores (hex[:8])
        stem = fpath.stem  # e.g. "40a9b28b_ja" or "40a9b28b_zh-CN"
        # split on first underscore to get pid
        parts = stem.split("_", 1)
        if len(parts) == 2 and parts[0] not in valid_ids:
            fpath.unlink()
            deleted.append(fpath.name)

    return {"deleted": deleted, "count": len(deleted)}


# ── legacy / upload endpoints ──────────────────────────────────────────────────

@app.post("/api/points/{pid}/tts")
async def generate_tts_legacy(pid: str, lang: str = "ja"):
    return await generate_audio(pid, lang)


@app.post("/api/points/{pid}/upload")
async def upload_audio(pid: str, file: UploadFile = File(...)):
    pts = _load()
    idx = next((i for i, p in enumerate(pts) if p.get("id") == pid), None)
    if idx is None:
        raise HTTPException(404, "Point not found")

    ext = Path(file.filename).suffix.lower() if file.filename else ".mp3"
    fname = f"{pid}{ext}"
    (AUDIO_BASE / fname).write_bytes(await file.read())

    pts[idx]["audio_file"] = fname
    _save(pts)
    return {"filename": fname}


@app.post("/api/points/{pid}/set_active_audio")
async def set_active_audio(pid: str, filename: str):
    pts = _load()
    idx = next((i for i, p in enumerate(pts) if p.get("id") == pid), None)
    if idx is None:
        raise HTTPException(404, "Point not found")
    pts[idx]["audio_file"] = filename
    _save(pts)
    return {"ok": True}


@app.get("/api/config_status")
async def config_status():
    ok = _openai_key()
    gk = _google_tts_key()
    logger.info(
        "config_status: OPENAI=%s GOOGLE_TTS=%s",
        (ok[:10] + "...") if ok else "(not set)",
        (gk[:10] + "...") if gk else "(not set)",
    )
    return {
        "openai": bool(ok),
        "google_tts": bool(gk),
    }


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = AUDIO_BASE / filename
    if not path.exists():
        raise HTTPException(404, "Audio file not found")
    return FileResponse(str(path))


# ── entry point ────────────────────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
