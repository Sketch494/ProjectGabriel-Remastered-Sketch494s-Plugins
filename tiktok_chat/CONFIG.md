# TikTok Live Chat plugin — `config.yml` fragment

Copy the block below into Gabriel's main **`config.yml`** under the existing top-level **`plugins:`** key (same level as `suno:`, `govee:`, `camera:`).

The plugin is **disabled by default** in `plugins/tiktok_chat/plugin.yml` — flip `enabled: true` there before the rest matters. Per-tool toggles auto-populate in `config/tools.yml` under `plugin_tools.tiktok_chat`.

Install into **Gabriel’s `.venv`** (same Python as `run.bat`), not a global interpreter:

```bash
uv pip install --python .venv/Scripts/python.exe TikTokLive "pyee>=11,<12"
```

(`pyee` 12+ breaks `TikTokLive`’s imports; pin `<12`.) Recent **tiktoklive** wheels put event classes in `TikTokLive.types.events` instead of `TikTokLive.events`; this plugin tries both.

> **Note:** `TikTokLive` scrapes TikTok's webcast endpoints. For unattended use you may want a sign-server API key (Eulerstream / SignAPI). Set `api_key` (or `api_key_env`) below if you have one.

---

## Block to paste

```yaml
  tiktok_chat:
    # Optional: auto-connect to this TikTok @handle on Gabriel startup.
    # Leave blank to require an explicit `connectTikTokChat` tool call.
    default_username: ""
    # If true and default_username is set, the plugin connects on startup.
    auto_connect: false

    # Optional sign-server API key (Eulerstream/SignAPI) for reliability.
    # Read from this env var if api_key is empty.
    api_key: ""
    api_key_env: "TIKTOK_API_KEY"

    # Ring buffer length for chat events (comments, gifts, etc.).
    buffer_size: 200

    # How chat flows into the AI's live session:
    #   buffer       — silent; AI must call getRecentTikTokChat
    #   live_silent  — periodically inject batches as background context (no auto-reply)
    #   live_reply   — same batches, but each ends a turn so the AI is prompted to respond
    # Default is live_reply so the AI both SEES chat and answers it out loud.
    relay_mode: live_reply

    # Live-mode batching cadence (seconds, minimum 2). Used by live_silent / live_reply.
    # Keep this >= 15 on live_reply so the AI can finish a response before the
    # next batch arrives — anything lower will constantly interrupt itself.
    relay_interval_seconds: 20
    # Don't flush a batch unless this many new lines have arrived. 2-3 keeps
    # the AI from taking a turn for every single comment when chat is sparse.
    relay_min_messages: 2

    # Event filters
    include_gifts: true
    include_follows: true
    include_likes: false
    include_shares: false

    # Prefix prepended to lines when relayed to the AI (e.g. "[TikTok] viewer: hi")
    prefix: "[TikTok]"
    # Drop comments shorter than this (keeps spam out of the buffer).
    filter_min_chars: 1
```

---

## Tool reference

| Tool | What it does |
|------|-------------|
| **`connectTikTokChat`** | Connect to a TikTok Live by username (`@handle` or bare handle). |
| **`disconnectTikTokChat`** | Disconnect and stop the relay loop. |
| **`getRecentTikTokChat`** | Pull buffered events. Args: `limit`, `sinceIndex`, `kinds` (`['comment','gift','follow','like','share']`). |
| **`getTikTokChatStatus`** | Connection state, username, relay mode, lifetime counts. |
| **`setTikTokRelayMode`** | Switch between `buffer` / `live_silent` / `live_reply` at runtime. Optional `intervalSeconds`. |
| **`clearTikTokChatBuffer`** | Wipe the local ring buffer (does not disconnect). |

Per-tool toggles in **`config/tools.yml`**:

```yaml
plugin_tools:
  tiktok_chat:
    connectTikTokChat: true
    disconnectTikTokChat: true
    getRecentTikTokChat: true
    getTikTokChatStatus: true
    setTikTokRelayMode: true
    clearTikTokChatBuffer: true
```

---

## How relay modes inject

* **`live_silent`** — batches new chat lines, formats them as `"[TikTok] <user>: <text>"`, and calls `live_session.send_client_content_safe(turn_complete=False)`. The AI sees the lines as user-role context but isn't forced to respond, the same pattern the social plugin uses for incoming text messages.
* **`live_reply`** — same batching but `turn_complete=True`, so the AI is prompted to actually answer chat at the end of each batch. Use a higher **`relay_interval_seconds`** (e.g. 15–30) here so the AI doesn't get interrupted every few seconds.
* **`buffer`** — never auto-injects. Best when the user doesn't want chat to drive the conversation; the AI can still pull lines on demand.

Switch modes at runtime with `setTikTokRelayMode` so you don't have to restart Gabriel to go from "silent watch" to "actively reading chat."

---

## Troubleshooting: `Failed to fetch room id from Webcast`

TikTokLive loads `https://www.tiktok.com/@HANDLE/live` and parses a **`room_id`** from the HTML. That step failed. Typical reasons:

1. **The account is not live** — The creator must be **broadcasting right now**. If they are offline or only posted a video, there is no live room id.
2. **Wrong username** — Use the **`@handle` from their profile URL** (e.g. `fenyx_the_chibi`), not a display name or a different platform’s handle.
3. **TikTok blocked or altered the page** for your IP/region — The library’s inner error may say something like *offline* vs *blocked*. Try another network or VPN if you suspect blocking.
4. **Sign server (optional)** — Set **`api_key`** / **`api_key_env`** in `config.yml` under `plugins.tiktok_chat` if you use Eulerstream (or similar) for higher reliability on later Webcast steps and rate limits.

5. **`AsyncClient(..., proxies=...)` errors** — Gabriel may ship **httpx 0.28+**, which removed that argument. The plugin patches TikTokLive’s HTTP client on load (`httpx_compat.py`); restart Gabriel after upgrading.

After changing config, restart Gabriel and connect again while the stream is **definitely live** in a normal browser.
