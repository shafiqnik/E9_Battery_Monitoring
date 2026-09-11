import os

CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.txt")

DEFAULTS = {
    "username": "contigo",
    "password": "C0nt1g0",
    "broker": "ra-net.contigo.com",
    "port": "7008",
    "topic": "/cell/#",
    "esn": "30AE7BE844CF",
    "vecima": "564543494D41",
    "header": "E1FFA1",
}

_ALIASES = {
    "username": ("username", "login", "user"),
    "password": ("password",),
    "broker": ("broker", "mqtt_broker", "mqttbroker"),
    "port": ("port", "mqtt_port", "portnumber"),
    "topic": ("topic", "mqtt_topic"),
    "esn": ("esn", "cfc", "ids", "device_id", "deviceid"),
    "vecima": ("vecima",),
    "header": ("header", "ble_header"),
}

_cache = None


class Config:
    def __init__(self, values):
        self.username = values["username"]
        self.password = values["password"]
        self.broker = values["broker"]
        self.port = values["port"]
        self.topic = values["topic"]
        self.esn = values["esn"]
        self.ids = values["ids"]
        self.vecima = values["vecima"]
        self.header = values["header"]
        self.header_bytes = values["header_bytes"]


def _read_file(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip().lower()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _pick(raw, *keys, default=""):
    for key in keys:
        val = raw.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def parse_ids(raw):
    parts = (
        str(raw or "")
        .upper()
        .replace(" ", "")
        .replace(":", "")
        .replace("'", "")
        .split(",")
    )
    ids = [p for p in parts if p]
    return ids or parse_ids(DEFAULTS["esn"])


def _parse_port(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return int(DEFAULTS["port"])


def _parse_hex(raw, fallback):
    cleaned = str(raw or "").upper().replace(" ", "").replace(":", "").replace("0X", "")
    try:
        data = bytes.fromhex(cleaned)
    except ValueError:
        data = bytes.fromhex(fallback)
        cleaned = fallback
    if not data:
        data = bytes.fromhex(fallback)
        cleaned = fallback
    return cleaned, data


def load_config(path=None, reload=False):
    global _cache
    path = path or CREDENTIALS_FILE
    if _cache is not None and not reload and path == CREDENTIALS_FILE:
        return _cache
    raw = _read_file(path) if os.path.isfile(path) else {}
    merged = {}
    for name, aliases in _ALIASES.items():
        merged[name] = _pick(raw, *aliases, default=DEFAULTS[name]) or DEFAULTS[name]
    merged["port"] = _parse_port(merged["port"])
    esn_raw = _pick(raw, *_ALIASES["esn"], default=DEFAULTS["esn"]) or DEFAULTS["esn"]
    merged["ids"] = parse_ids(esn_raw)
    merged["esn"] = ",".join(merged["ids"])
    vecima, _ = _parse_hex(merged["vecima"], DEFAULTS["vecima"])
    header, header_bytes = _parse_hex(merged["header"], DEFAULTS["header"])
    merged["vecima"] = vecima
    merged["header"] = header
    merged["header_bytes"] = header_bytes
    cfg = Config(merged)
    if path == CREDENTIALS_FILE:
        _cache = cfg
    return cfg


def load_credentials(path=None):
    cfg = load_config(path)
    return cfg.username, cfg.password
