# Govee plugin — `config.yml` fragment

Copy the block below into your Gabriel **`config.yml`** under the top-level **`plugins:`** key as **`govee:`** (same level as `suno:` and other plugins). Indentation must stay valid YAML (two spaces per level under `plugins:`).

Enable the plugin in **`plugins/govee/plugin.yml`** and tool names in **`config/tools.yml`** under **`plugin_tools.govee`**.

Do **not** commit real API keys. Prefer environment variables:

- Set **`GOVEE_API_KEY`** and leave **`api_keys: []`**, or  
- Use **`api_keys`** only in a local, untracked override.

---

## Full example (replace placeholders)

```yaml
  govee:
    api_keys:
      - "YOUR-GOVEE-LAN-API-KEY"
    api_key_env: GOVEE_API_KEY
    api_key_env_list: ""

    discord_webhook: ""
    discord_webhook_env: ""

    device_refresh_seconds: 300
    state_poll_seconds: 0
    command_cooldown_ms: 0
    min_command_interval_ms: 30
    light_control_min_interval_ms: 45
    max_concurrent_requests: 4

    default_brightness_min: 1
    default_brightness_max: 100

    block_scene_changes: false
    block_power_off: false
    block_brightness_changes: false
    block_color_changes: false
    require_confirmation_for_power_off: false
    require_confirmation_for_scene: false
    admin_override_param: false

    allowed_devices: []
    blocked_devices: []
    blocked_groups: []

    device_groups:
      bedroom:
        - "AA:BB:CC:DD:EE:FF:00:11"
      desk: []

    room_restrictions: {}
    room_presets: {}

    device_permissions: {}

    color_tolerance_default: 12
    blocked_colors: []
    blocked_scenes: []
    blocked_scene_categories: []

    mqtt_enabled: false
    mqtt_host: mqtt.openapi.govee.com
    mqtt_port: 8883

    favorites_path: scene_favorites.json

    automations: []

    music_sync:
      enabled: false
      poll_interval_ms: 120
      group: bedroom
      device_ids: []
      mode: hue_plus_pulse
      hue_period_seconds: 48
      saturation: 1.0
      value: 1.0
      brightness_base: 55
      brightness_swing: 28
      pulse_hz: 0.85
      brightness_min: 8
      brightness_max: 100
      bypass_color_blocks: true

    emergency_fallback:
      enabled: false
      consecutive_failures: 5
      cooldown_seconds: 300
      source_label: emergency_fallback
      apply:
        group: bedroom
        power_on: true
        brightness: 80
        scene_name: "Leisure"

    debug: false
    analytics_enabled: true
```

---

## Minimal starter (keys + one group)

```yaml
  govee:
    api_keys: []
    api_key_env: GOVEE_API_KEY
    device_groups:
      living_room:
        - "YOUR-DEVICE-ID-HERE"
```

Run once and use **`listGoveeDevices`** (or refresh the device cache) to fill real IDs.

---

## Notes

| Key | Purpose |
|-----|---------|
| **`min_command_interval_ms`** | Minimum gap between any two HTTP calls to Govee. |
| **`light_control_min_interval_ms`** | Extra pacing for **`POST /device/control`** (colors, brightness, scenes). |
| **`state_poll_seconds`** | If `> 0`, periodically fetches device state (uses **`max_concurrent_requests`** workers). |
| **`favorites_path`** | JSON file under **`data/plugins/govee/`** mapping shortcut → Govee scene name (see **`scene_favorites.example.json`**). |
| **`music_sync`** | Drives lights from **playback position** (local **`playMusic`** + Suno). Not FFT beat detection. Set **`enabled: true`** and restart Gabriel. |
| **`emergency_fallback`** | After repeated API errors, applies **`apply`** once per cooldown. |

Optional JSON beside the plugin: **`restrictions.json`**, **`colors_extra.json`**. Data-dir override: **`data/plugins/govee/restrictions.override.json`**.
