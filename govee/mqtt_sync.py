"""Optional MQTT subscriber (Govee device events)."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

_LOG = logging.getLogger(__name__)


class GoveeMqttBridge:
    """Subscribes to Govee OpenAPI MQTT (TLS). Runs paho loop in a daemon thread."""

    def __init__(
        self,
        api_key: str,
        *,
        host: str = "mqtt.openapi.govee.com",
        port: int = 8883,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._api_key = api_key
        self._host = host
        self._port = port
        self._on_event = on_event
        self._thread: threading.Thread | None = None
        self._client = None

    def start(self) -> None:
        try:
            import paho.mqtt.client as mqtt  # type: ignore
        except ImportError:
            _LOG.warning("govee: paho-mqtt not installed, MQTT sync disabled")
            return

        def _on_connect(client, _userdata, _flags, rc):  # noqa: ANN001
            if rc == 0:
                _LOG.info("govee mqtt connected")
                client.subscribe(self._api_key)
            else:
                _LOG.warning("govee mqtt connect failed rc=%s", rc)

        def _on_message(_client, _userdata, msg):  # noqa: ANN001
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except Exception:
                return
            if self._on_event:
                try:
                    self._on_event(payload)
                except Exception as e:
                    _LOG.debug("govee mqtt handler error: %s", e)

        client = mqtt.Client()  # type: ignore[call-arg]
        client.username_pw_set(self._api_key, self._api_key)
        client.tls_set()
        client.on_connect = _on_connect
        client.on_message = _on_message

        def _loop():
            try:
                client.connect(self._host, self._port, 60)
                client.loop_forever()
            except Exception as e:
                _LOG.warning("govee mqtt loop ended: %s", e)

        self._client = client
        self._thread = threading.Thread(target=_loop, daemon=True, name="govee-mqtt")
        self._thread.start()

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
