# Camera plugin — `config.yml` fragment

Copy the block below into Gabriel's main **`config.yml`** under the existing top-level **`plugins:`** key (same level as `suno:`, `govee:`, `mood:`).

The plugin itself is **disabled by default** in **`plugins/camera/plugin.yml`** — flip `enabled: true` there before any of this matters. Per-tool toggles auto-populate into **`config/tools.yml`** under **`plugin_tools.camera`** on first launch.

Install the runtime deps once:

```bash
pip install opencv-python Pillow fastapi uvicorn
```

`fastapi` and `uvicorn` are only needed if you want the in-browser preview. The plugin still works without them; the preview just won't start.

---

## Block to paste

```yaml
  camera:
    # OS camera index. 0 = first available device (built-in webcam on most laptops).
    default_device: 0

    # OpenCV backend hint. "auto" lets cv2 pick. On Windows "dshow" or "msmf"
    # often works better than auto for some webcams; on Linux use "v4l2"; on
    # macOS use "avfoundation".
    default_backend: auto

    # Streaming cadence — milliseconds between frames pushed into the Gemini
    # Live video channel. Lower = smoother but burns more tokens. Match this
    # to your screen-vision interval if you want similar pacing.
    frame_interval_ms: 1000

    # Pixel cap on the longer image edge. Frames are downscaled to fit before
    # JPEG encoding. 1024 is a good default; drop to 768 on Gemini 3.1 to save
    # tokens. The screen-vision loop uses 1024 by default.
    max_size: 1024

    # JPEG quality 1-95. Lower = smaller payload, lossier image.
    jpeg_quality: 75

    # Selfie cams look mirrored to the AI by default. Flip horizontally so the
    # user appears un-mirrored.
    mirror: true

    # Skip frames while the AI is speaking or music is playing (matches the
    # screen-vision pause_on_output behavior). Live music_gen is excluded so
    # the AI can still see while it improvises.
    pause_on_speaking: true

    # Auto-close the camera after this many seconds of streaming. 0 = never
    # auto-close. Useful as a "you forgot to turn it off" failsafe.
    auto_close_seconds: 0

    # If true (default), `openCamera` rejects calls that don't pass
    # `userConfirmed: true`. Forces the AI to confirm with the user before
    # opening the webcam. Disable only if you trust the model fully.
    require_user_confirm: true

    # In-browser MJPEG preview at http://localhost:<port>/camera. Mirrors
    # the existing vision_server.py pattern for the YOLO tracker. Requires
    # fastapi+uvicorn — without them the preview just stays off.
    preview:
      enabled: true
      port: 8768
      fps: 15
```

**Preview UI:** once running, open `http://localhost:8768/camera` in any browser. The page MJPEG-streams the same JPEG frames the AI is seeing, plus a small status pane (active flag, device index, frame count, frame size, open seconds). The stream stays connected even after `closeCamera` — it just goes blank until the next open.

---

## Tool reference

| Tool | What it does |
|------|-------------|
| **`openCamera`** | Start streaming a webcam into the live video channel. Args: `deviceIndex`, `frameIntervalMs`, `maxSize`, `mirror`, `backend`, `userConfirmed`. |
| **`closeCamera`** | Stop streaming and release the device. |
| **`captureCameraSnapshot`** | One-shot frame. Pass `sendToVision: true` to push it once. |
| **`listCameras`** | Probes indices 0..maxIndex and returns whichever respond, with reported width/height. |
| **`getCameraStatus`** | Returns active flag, current device index, frames sent, open seconds, etc. |

Per-tool toggles in **`config/tools.yml`**:

```yaml
plugin_tools:
  camera:
    openCamera: true
    closeCamera: true
    captureCameraSnapshot: true
    listCameras: true
    getCameraStatus: true
```

---

## How it integrates

Frames are JPEG-encoded and pushed onto **`live_session._out_queue`** as **`("video", jpeg_bytes)`** — the exact channel the screen-capture vision loop already feeds. The session's send loop forwards them as **`types.Blob(mime_type="image/jpeg")`** via `send_realtime_input(video=...)`, so no Gemini Live session changes are needed.

If both the screen-capture loop and the camera plugin are pushing simultaneously they share the queue; on backpressure the older frame is dropped (same eviction policy as the screen-capture loop).
