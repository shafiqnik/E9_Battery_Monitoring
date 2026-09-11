from credentials import load_config

_cfg = load_config()
VECIMA = _cfg.vecima
HEADER = _cfg.header_bytes


def parse_e9_battery(data_hex, modelstr=None):
    """Parse E9 Device Info Frame (AD 0x16, UUID 0xFFE1, frame 0xA1). Returns battery % or None."""
    if modelstr is not None and modelstr != "Bledevice":
        return None
    hex_str = "".join(str(data_hex).split()).replace(":", "").upper()
    if not hex_str:
        return None
    try:
        payload = bytes.fromhex(hex_str)
    except ValueError:
        return None
    i = 0
    while i < len(payload):
        length = payload[i]
        if length == 0 or i + 1 + length > len(payload):
            break
        ad_type = payload[i + 1]
        value = payload[i + 2 : i + 1 + length]
        i += 1 + length
        if ad_type != 0x16 or len(value) < 5 or value[:3] != HEADER:
            continue
        battery = value[4]
        if battery > 100:
            continue
        return {
            "battery": battery,
            "version": value[3],
            "firmware": "new" if VECIMA in hex_str else "legacy",
        }
    return None


def battery_percent(data_hex, modelstr=None):
    parsed = parse_e9_battery(data_hex, modelstr)
    return None if parsed is None else parsed["battery"]
