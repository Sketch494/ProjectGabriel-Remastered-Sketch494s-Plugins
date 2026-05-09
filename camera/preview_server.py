"""Tiny MJPEG preview server for the camera plugin.

Mirrors the pattern in `vision_server.py` so the user can open
`http://localhost:8768/camera` in a browser and see exactly what the AI is
seeing through `openCamera`. All frames flow through `update_frame()`, which
the `CameraStream` calls after each successful capture.

The server runs in a daemon thread (uvicorn) so a process exit always cleans
it up; teardown also flips an `_active` flag so the stream's MJPEG generator
exits its yield loop and existing browsers reconnect cleanly.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


_state: dict[str, Any] = {
    "jpeg": None,
    "frame_index": 0,
    "lock": threading.Lock(),
    "thread": None,
    "active": False,
    "device_index": None,
    "frames_sent": 0,
    "last_size": None,
    "started_at": None,
    "fps": 15,
    "port": 8768,
    "app_name": "Gabriel",
}


_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<title>{{APP_NAME}} Camera Preview</title>
<meta http-equiv="cache-control" content="no-cache">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #111; color: #eee; font-family: monospace; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
  h1 { padding: 8px 0; font-size: 16px; color: #0ff; }
  .container { display: flex; gap: 12px; padding: 8px; max-width: 100%; flex-wrap: wrap; justify-content: center; }
  .stream { border: 2px solid #333; background: #000; max-width: 70vw; max-height: 80vh; }
  .stats { background: #1a1a1a; border: 1px solid #333; padding: 12px; min-width: 240px; border-radius: 4px; }
  .stats h2 { color: #0ff; font-size: 14px; margin-bottom: 8px; border-bottom: 1px solid #333; padding-bottom: 4px; }
  .stat { display: flex; justify-content: space-between; padding: 3px 0; font-size: 13px; }
  .stat .label { color: #888; }
  .stat .value { color: #0ff; font-weight: bold; }
  .stat .value.warn { color: #f80; }
  .stat .value.bad { color: #f44; }
  .footer { color: #666; font-size: 11px; padding: 8px; }
</style>
</head>
<body>
<h1>{{APP_NAME}} Camera Preview</h1>
<div class="container">
  <img class="stream" src="/camera/stream" alt="camera stream" />
  <div class="stats">
    <h2>Camera Stream</h2>
    <div id="stat-lines">Loading...</div>
  </div>
</div>
<div class="footer">This is the same JPEG feed sent to Gemini Live via openCamera.</div>
<script>
async function poll() {
  try {
    const r = await fetch('/camera/data');
    const d = await r.json();
    let html = '';
    html += stat('Active', d.active ? 'YES' : 'no', d.active ? 'value' : 'value warn');
    html += stat('Device', d.device_index ?? '-');
    html += stat('Frames Sent', d.frames_sent);
    html += stat('Last Frame', d.last_size ? d.last_size.join('x') : '-');
    html += stat('Open Seconds', d.open_seconds.toFixed(1));
    document.getElementById('stat-lines').innerHTML = html;
  } catch(e) {}
  setTimeout(poll, 500);
}
function stat(label, value, cls) {
  cls = cls || 'value';
  return '<div class="stat"><span class="label">' + label + '</span><span class="' + cls + '">' + value + '</span></div>';
}
poll();
</script>
</body>
</html>"""


def _build_html(app_name: str) -> str:
    import html as _html
    return _HTML_TEMPLATE.replace("{{APP_NAME}}", _html.escape(app_name))


def update_frame(jpeg: bytes, *, device_index: int | None, frames_sent: int, last_size: tuple[int, int] | None, started_at: float | None) -> None:
    """Push a JPEG frame for the preview to serve. Cheap, lock-protected."""
    with _state["lock"]:
        _state["jpeg"] = jpeg
        _state["frame_index"] += 1
        _state["device_index"] = device_index
        _state["frames_sent"] = frames_sent
        _state["last_size"] = last_size
        _state["started_at"] = started_at


def clear_frame() -> None:
    with _state["lock"]:
        _state["jpeg"] = None
        _state["device_index"] = None
        _state["last_size"] = None
        _state["started_at"] = None


def is_running() -> bool:
    t = _state.get("thread")
    return bool(t is not None and t.is_alive())


def start_server(*, port: int = 8768, fps: int = 15, app_name: str = "Gabriel") -> bool:
    """Start the preview HTTP server in a daemon thread. Idempotent.

    Returns True if running (already or after start), False if FastAPI/uvicorn
    are unavailable.
    """
    if is_running():
        return True
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
        import uvicorn
    except Exception:
        logger.warning("camera preview server requires fastapi+uvicorn — disabled")
        return False

    _state["fps"] = max(1, int(fps))
    _state["port"] = int(port)
    _state["app_name"] = str(app_name)

    html_page = _build_html(app_name)
    vapp = FastAPI(title=f"{app_name} Camera Preview")

    @vapp.get("/camera", response_class=HTMLResponse)
    async def camera_page():
        return html_page

    @vapp.get("/camera/data")
    async def camera_data():
        with _state["lock"]:
            started = _state["started_at"]
            return JSONResponse({
                "active": _state["active"],
                "device_index": _state["device_index"],
                "frames_sent": _state["frames_sent"],
                "last_size": list(_state["last_size"]) if _state["last_size"] else None,
                "open_seconds": (time.time() - started) if started else 0.0,
                "frame_index": _state["frame_index"],
            })

    @vapp.get("/camera/stream")
    async def camera_stream():
        sleep_s = 1.0 / max(1, _state["fps"])

        def generate():
            last_index = -1
            blank_emitted = False
            while True:
                with _state["lock"]:
                    frame = _state["jpeg"]
                    idx = _state["frame_index"]
                if frame is not None and idx != last_index:
                    last_index = idx
                    blank_emitted = False
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
                elif not blank_emitted and frame is None:
                    blank_emitted = True
                time.sleep(sleep_s)

        return StreamingResponse(
            generate(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    _state["active"] = True

    def _run():
        try:
            uvicorn.run(vapp, host="0.0.0.0", port=int(port), log_level="warning")
        except Exception as e:  # pragma: no cover - port collision, etc.
            logger.warning(f"camera preview server stopped: {e}")
        finally:
            _state["active"] = False

    t = threading.Thread(target=_run, daemon=True, name="camera-preview-server")
    _state["thread"] = t
    t.start()
    logger.info(f"camera preview server started on http://localhost:{port}/camera")
    return True


def mark_active(active: bool) -> None:
    """Update the 'Active' badge shown on the preview page."""
    _state["active"] = bool(active)
