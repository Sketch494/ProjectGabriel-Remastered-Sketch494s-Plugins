# YouTube playback plugin — `config.yml` fragment

Copy the block below into Gabriel's main **`config.yml`** under the existing top-level **`plugins:`** key (same level as `suno:`, `govee:`, `camera:`, `tiktok_chat:`).

The plugin is **disabled by default** in `plugins/youtube/plugin.yml` — flip `enabled: true` there to load it. Per-tool toggles auto-populate in `config/tools.yml` under `plugin_tools.youtube` on first launch.

Install the runtime deps once:

```bash
pip install yt-dlp imageio-ffmpeg
```

`imageio-ffmpeg` is already pulled in by the Suno plugin; if you have a system `ffmpeg` on PATH that works too. Without ffmpeg the plugin will refuse to start a stream.

---

## Block to paste

```yaml
  youtube:
    # Default playback volume (0-200, 100 = unity).
    default_volume: 80

    # Number of search results returned by `searchYouTube` when limit isn't set.
    search_limit: 5

    # Reject videos longer than this many seconds. 0 disables the cap.
    max_duration_seconds: 1800

    # If true, refuse to play live streams.
    block_livestreams: false

    # yt-dlp format selector. "bestaudio/best" is the safe default and
    # automatically picks an audio-only stream when available.
    ytdlp_format: "bestaudio/best"

    # Optional: path to a Netscape cookies.txt for age-restricted / member content.
    cookies_file: ""

    # Try yt-dlp's geo-bypass when the video is region-blocked.
    geo_bypass: true

    # Number of finished tracks kept in memory for `getYouTubeStatus`.
    history_size: 50
```

---

## Tool reference

| Tool | What it does |
|------|-------------|
| **`playYouTube`** | Resolve and play. Args: `query` (search/URL/ID), `autoSearch` (bool). |
| **`searchYouTube`** | Top results without playing. Args: `query`, `limit`. |
| **`queueYouTube`** | Append to the queue. Falls back to `playYouTube` when nothing is playing. |
| **`skipYouTube`** | Skip current and start the next queued track. |
| **`stopYouTube`** | Stop and clear the queue. |
| **`pauseYouTube`** / **`resumeYouTube`** | Toggle without dropping the stream. |
| **`setYouTubeVolume`** | 0-200 (100 = unity). |
| **`clearYouTubeQueue`** | Drop the queue, keep the current track. |
| **`getYouTubeStatus`** | Title, uploader, position, queue length, etc. |

Per-tool toggles in **`config/tools.yml`**:

```yaml
plugin_tools:
  youtube:
    playYouTube: true
    searchYouTube: true
    queueYouTube: true
    skipYouTube: true
    stopYouTube: true
    pauseYouTube: true
    resumeYouTube: true
    setYouTubeVolume: true
    clearYouTubeQueue: true
    getYouTubeStatus: true
```

---

## How it integrates

1. `yt-dlp` resolves a stream URL (or runs a `ytsearchN` for free-text queries).
2. `ffmpeg` decodes the stream to 48 kHz / stereo / s16le on stdout.
3. A worker thread reads ~100 ms PCM chunks and writes them to a PyAudio output stream on the same `output_device_index` the rest of the host uses.
4. The manager flips `audio.set_external_music_active(True)` while playing, so the AI's spoken voice ducks (same path local music + Suno already use) and downstream features (idle detection, vision-pause-on-output) behave consistently.

Notes:

* The play queue is purely in-memory — it doesn't persist across restarts.
* Pause keeps the ffmpeg subprocess alive so resume is instant. Stop kills it. After ~30 minutes of being paused the upstream HTTP connection may time out; restart with `playYouTube` if that happens.
* `cookies_file` is only needed for age-restricted or member-only content. Export from your browser via a "cookies.txt" extension.
