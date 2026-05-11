# YouTube playback plugin — `config.yml` fragment

Copy the block below into Gabriel's main **`config.yml`** under the existing top-level **`plugins:`** key (same level as `suno:`, `govee:`, `camera:`, `tiktok_chat:`).

The plugin is **disabled by default** in `plugins/youtube/plugin.yml` — flip `enabled: true` there to load it. Per-tool toggles auto-populate in `config/tools.yml` under `plugin_tools.youtube` on first launch.

Install the runtime deps once:

```bash
pip install yt-dlp imageio-ffmpeg chat-downloader
```

`chat-downloader` is only needed for **`watchYouTubeLiveChat`** / live-chat tools; playback works with yt-dlp alone.

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

    # yt-dlp timeouts (seconds). Prevents hung tools from wedging the AI session.
    resolve_timeout_seconds: 120
    search_timeout_seconds: 90

    # Save a copy of each played track to disk while streaming (non-blocking).
    # Files are named ``<video_id>.<ext>`` under ``save_dir`` (skipped if already exists).
    save_while_playing: true
    save_dir: "sfx/youtube"

    # VRChat chatbox when YouTube is playing (see README below).
    chatbox_enabled: true
    # Optional Python ``str.format`` template (max 144 chars after format).
    # Placeholders: {header} {title} {uploader} {bar} {time} {queue_suffix} {video_id}
    # chatbox_template: "{header} {title}\n{bar}\n{time}{queue_suffix}"

    # Number of finished tracks kept in memory for `getYouTubeStatus`.
    history_size: 50

    # ---- Live / replay chat (optional `pip install chat-downloader`) ----
    # Ring buffer of normalized chat messages for tool polling.
    live_chat_buffer_size: 300
    # How chat reaches the model — see "Live chat relay" below.
    # Default live_reply: model speaks back to chat on each batch.
    live_chat_relay_mode: live_reply
    live_chat_relay_interval_seconds: 15
    live_chat_relay_min_messages: 1
    live_chat_prefix: "[YT Chat]"
    # Persist chat to Gabriel memory (same backend as saveMemory). Uses viewer names in text.
    live_chat_save_memories: true
    live_chat_memory_interval_seconds: 20
    live_chat_memory_type: short_term   # long_term | short_term | quick_note
    live_chat_memory_category: youtube_live_chat
    live_chat_memory_max_chars: 6000
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
| **`getYouTubeStatus`** | Title, uploader, position, queue length, etc. Includes nested **`liveChat`** (watching, buffer size, relay mode, memory save flags). |
| **`watchYouTubeLiveChat`** | Start ingesting chat for a URL/video ID (or the current track). Requires chat-downloader. |
| **`stopYouTubeLiveChat`** | Stop reader + relay; playback unchanged. |
| **`getYouTubeLiveChatMessages`** | Recent buffered messages (`limit`, optional `sinceIndex` for polling). |
| **`clearYouTubeLiveChat`** | Clear buffer + indices; reader keeps running if active. |
| **`setYouTubeLiveChatRelayMode`** | `buffer` \| `live_silent` \| `live_reply`; optional `intervalSeconds`. |

Per-tool toggles in **`config/tools.yml`**:

```yaml
plugin_tools:
  youtube:
    clearYouTubeLiveChat: true
    clearYouTubeQueue: true
    getYouTubeLiveChatMessages: true
    getYouTubeStatus: true
    pauseYouTube: true
    playYouTube: true
    queueYouTube: true
    resumeYouTube: true
    searchYouTube: true
    setYouTubeLiveChatRelayMode: true
    setYouTubeVolume: true
    skipYouTube: true
    stopYouTube: true
    stopYouTubeLiveChat: true
    watchYouTubeLiveChat: true
```

---

## How it integrates

1. `yt-dlp` resolves a stream URL (or runs a `ytsearchN` for free-text queries).
2. `ffmpeg` decodes the stream to 48 kHz / stereo / s16le on stdout.
3. A worker thread reads ~100 ms PCM chunks and writes them to a PyAudio output stream on the same `output_device_index` the rest of the host uses.
4. The manager flips `audio.set_external_music_active(True)` while playing, so the AI's spoken voice ducks (same path local music + Suno already use). **Starting local music via `playMusic` now clears that flag first**, so SFX files no longer leave YouTube's external-music state stuck or fight the chatbox.

Notes:

* **Chatbox:** With no built-in music progress, the host shows plugin chatbox sources. YouTube now **hides its banner whenever pygame local music is playing** (SFX / `playMusic`), so the VRChat chatbox matches what you hear. Customize the YouTube line with `chatbox_template` under `plugins.youtube` (see CONFIG block).
* **Background save:** Each started track triggers a background yt-dlp download to `save_dir` (default `sfx/youtube`), keyed by `video_id`, without blocking playback.
* The play queue is purely in-memory — it doesn't persist across restarts (saved files do).
* Pause keeps the ffmpeg subprocess alive so resume is instant. Stop kills it. After ~30 minutes of being paused the upstream HTTP connection may time out; restart with `playYouTube` if that happens.
* `cookies_file` is only needed for age-restricted or member-only content. Export from your browser via a "cookies.txt" extension.

* **Live chat → memories:** With **`live_chat_save_memories: true`** (default), while **`watchYouTubeLiveChat`** is active the host periodically writes **`short_term`** memories (by default) containing viewer display names and their messages, keyed like `yt_lc_<videoId>_<firstIdx>_<lastIdx>`. Requires **`memory.enabled`** in main `config.yml` and a working memory backend. Use **`long_term`** only if you want chat retained past short-term TTL; busy chats generate many rows.

---

## Live chat persistent memory

| Key | Purpose |
|-----|---------|
| **`live_chat_save_memories`** | When true (default), flush buffered chat into the memory DB on an interval while watching. |
| **`live_chat_memory_interval_seconds`** | Minimum seconds between memory batches (default 20). |
| **`live_chat_memory_type`** | `short_term` (default, ~7-day TTL), `quick_note` (~6 h), or `long_term`. |
| **`live_chat_memory_category`** | Stored category string (default `youtube_live_chat`). |
| **`live_chat_memory_max_chars`** | Max stored body length per batch (default 6000). |

---

## Live chat relay

Modes (`plugins.youtube.live_chat_relay_mode` or **`setYouTubeLiveChatRelayMode`**):

| Mode | Behavior |
|------|----------|
| **`live_reply`** *(default)* | Batched chat is injected with **turn complete**, so the model typically **replies out loud** to viewers on each interval (after `live_chat_relay_min_messages` new lines). Use `live_chat_relay_interval_seconds` so she can finish speaking before the next batch (often 12–20s). |
| **`live_silent`** | Same batched injection **without** forcing a completed turn — background context only. |
| **`buffer`** | Chat is only visible via **`getYouTubeLiveChatMessages`**. Nothing is injected automatically. |

Prefix each line with `live_chat_prefix` so transcripts stay readable. Injection uses the same **`send_client_content_safe`** path as other mid-session context (including Gemini 3.1 realtime-input behavior).
