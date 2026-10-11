"""Settings, read from environment variables (or a .env file next to this one)."""
import os
from pathlib import Path

_env = Path(__file__).with_name(".env")
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_TLS = os.getenv("MQTT_TLS", "1") == "1"
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).with_name("insole.db")))

FS = 100                 # node sample rate, Hz
WINDOW_S = 10            # seconds of signal kept per node for analysis
ANALYSIS_EVERY_S = 1.0   # how often metrics are recomputed
AUTO_CUE_COOLDOWN_S = 5  # minimum gap between automatic cues to one node
