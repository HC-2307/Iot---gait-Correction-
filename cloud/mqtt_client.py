"""Shared MQTT connection setup."""
import ssl
import uuid

import paho.mqtt.client as mqtt

import config


def make_client(prefix: str) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{prefix}-{uuid.uuid4().hex[:6]}")
    if config.MQTT_USER:
        client.username_pw_set(config.MQTT_USER, config.MQTT_PASS or None)  # flespi: token, no password
    if config.MQTT_TLS:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.reconnect_delay_set(1, 10)
    return client


def topic(device: str, kind: str) -> str:
    return f"insole/{device}/{kind}"
