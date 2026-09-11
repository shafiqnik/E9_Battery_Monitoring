#!/usr/bin/env python3
"""Minew E9 BLE disconnection prediction.

Pipeline: raw advertisements → time-series features → ML probability.

RSSI is never used alone. Packet reception vs the expected ~3 s advertising
interval is a primary feature, together with RSSI trend/variability, gaps,
and battery decline. The supervised target is:

    Will this beacon become undetectable in the next H minutes,
    given the previous L minutes of history?

A beacon is disconnected when no valid E9 advertisement arrives for
N advertising intervals (default 20 × 3 s = 60 s).

Usage:
  python disconnection_predictor.py collect
  python disconnection_predictor.py train
  python disconnection_predictor.py predict
  python disconnection_predictor.py collect --predict
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import statistics
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable

from Battery import parse_e9_battery
from credentials import load_config, load_credentials

_cfg = load_config()
BROKER = _cfg.broker
MQTT_PORT = _cfg.port
TOPIC = _cfg.topic

ADV_INTERVAL_SEC = 3.0
DISCONNECT_INTERVALS = 20  # N × 3 s → 60 s with no ad = disconnected
LOOKBACK_SEC = 180  # 3 minutes of history (valid range 1–5 min)
HORIZON_SEC = 600  # predict next 10 minutes (also 5 / 30)
SAMPLE_STEP_SEC = 15
AT_RISK = 40.0
LIKELY = 70.0

ADS_LOG = "disconnection_ads.jsonl"
MODEL_FILE = "disconnection_model.pkl"
METRICS_FILE = "disconnection_metrics.json"
OUTCOMES_LOG = "disconnection_outcomes.jsonl"
LABELS_LOG = "disconnection_labels.jsonl"
SUITABLE_ACCURACY = 0.80
SUITABLE_MIN_OUTCOMES = 20
RETRAIN_COOLDOWN_SEC = 120

RSSI_KEYS = ("rssi", "RSSI", "rssi_value", "signal", "dbm", "rssiDb")
MAC_KEYS = ("ble_addr", "bleAddr", "mac", "bdaddr", "addr")
GW_KEYS = ("gmac", "gw_mac", "gateway", "gateway_mac", "mac", "device_id", "dev_id")
LOC_KEYS = ("location", "loc", "site", "area", "lat", "latitude")
TS_KEYS = ("timestamp", "time", "ts", "datetime")


@dataclass
class Advertisement:
    ts: float
    ble_addr: str
    rssi: float | None = None
    battery: int | None = None
    version: int | None = None
    firmware: str | None = None
    gateway_id: str = ""
    location: str = ""
    modelstr: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Advertisement":
        return cls(
            ts=float(obj["ts"]),
            ble_addr=str(obj["ble_addr"]),
            rssi=_as_float(obj.get("rssi")),
            battery=_as_int(obj.get("battery")),
            version=_as_int(obj.get("version")),
            firmware=obj.get("firmware"),
            gateway_id=str(obj.get("gateway_id") or ""),
            location=str(obj.get("location") or ""),
            modelstr=str(obj.get("modelstr") or ""),
            extras=obj.get("extras") or {},
        )


FEATURE_NAMES = [
    "rssi_now",
    "rssi_mean",
    "rssi_min",
    "rssi_max",
    "rssi_std",
    "rssi_var",
    "rssi_slope",
    "rssi_range",
    "rssi_degrade_sec",
    "packets_received",
    "packets_expected",
    "packet_ratio",
    "missing_ratio",
    "time_since_last",
    "longest_gap",
    "mean_gap",
    "gap_std",
    "battery_now",
    "battery_slope",
    "battery_drop",
    "tx_power",
    "lookback_sec",
]


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    n = _as_float(value)
    return None if n is None else int(n)


def _first(obj: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def device_list(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, dict):
        lst = obj.get("device_list")
        if isinstance(lst, list):
            return [x for x in lst if isinstance(x, dict)]
        found: list[dict[str, Any]] = []
        for v in obj.values():
            found.extend(device_list(v))
        return found
    if isinstance(obj, list):
        found = []
        for v in obj:
            found.extend(device_list(v))
        return found
    return []


def _parse_ts(value: Any, fallback: float) -> float:
    if value is None or value == "":
        return fallback
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts / 1000.0 if ts > 1e12 else ts
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt.replace("Z", "")).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "")).timestamp()
    except ValueError:
        return fallback


def extract_gateway(msg: dict[str, Any]) -> tuple[str, str]:
    gw = str(_first(msg, GW_KEYS) or "")
    loc = _first(msg, LOC_KEYS)
    if loc is None and "longitude" in msg:
        loc = f"{msg.get('latitude')},{msg.get('longitude')}"
    return gw, str(loc or "")


def extract_advertisement(
    device: dict[str, Any],
    when: float,
    gateway_id: str = "",
    location: str = "",
) -> Advertisement | None:
    mac = str(_first(device, MAC_KEYS) or "").strip()
    if not mac:
        return None
    data = str(device.get("data") or device.get("raw") or device.get("adv") or "")
    model = device.get("modelstr")
    parsed = parse_e9_battery(data, model) if data else None
    extras = {}
    for key, val in device.items():
        if key in MAC_KEYS or key in RSSI_KEYS or key in ("data", "raw", "adv", "device_list"):
            continue
        if isinstance(val, (str, int, float, bool)) or val is None:
            extras[key] = val
    ts = _parse_ts(_first(device, TS_KEYS), when)
    return Advertisement(
        ts=ts,
        ble_addr=mac.upper(),
        rssi=_as_float(_first(device, RSSI_KEYS)),
        battery=None if parsed is None else parsed["battery"],
        version=None if parsed is None else parsed["version"],
        firmware=None if parsed is None else parsed["firmware"],
        gateway_id=gateway_id,
        location=location,
        modelstr=str(model or ""),
        extras=extras,
    )


def ingest_mqtt_payload(payload: str | dict[str, Any], when: float | None = None) -> list[Advertisement]:
    when = time.time() if when is None else when
    if isinstance(payload, str):
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            return []
    else:
        msg = payload
    if not isinstance(msg, dict):
        return []
    gw, loc = extract_gateway(msg)
    msg_ts = _parse_ts(_first(msg, TS_KEYS), when)
    ads = []
    for device in device_list(msg):
        ad = extract_advertisement(device, msg_ts, gw, loc)
        if ad:
            ads.append(ad)
    return ads


def _lin_slope(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    x0 = xs[0]
    rel = [x - x0 for x in xs]
    mean_x = sum(rel) / len(rel)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in rel)
    if var_x <= 1e-9:
        return 0.0
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(rel, ys))
    return cov / var_x


def _degrade_duration(times: list[float], rssi: list[float]) -> float:
    """Seconds of trailing RSSI decline (not a single low sample)."""
    if len(rssi) < 3:
        return 0.0
    slope = _lin_slope(times, rssi)
    if slope >= -0.01:
        return 0.0
    start = times[0]
    mid = (rssi[0] + rssi[len(rssi) // 2]) / 2.0
    for i, value in enumerate(rssi):
        if value <= mid - 2.0:
            start = times[i]
            break
    return max(0.0, times[-1] - start)


def features_from_window(
    ads: list[Advertisement],
    now: float,
    lookback_sec: float = LOOKBACK_SEC,
    adv_interval: float = ADV_INTERVAL_SEC,
) -> dict[str, float]:
    window = [a for a in ads if now - lookback_sec <= a.ts <= now]
    window.sort(key=lambda a: a.ts)
    empty = {name: 0.0 for name in FEATURE_NAMES}
    empty["time_since_last"] = lookback_sec
    empty["missing_ratio"] = 1.0
    empty["lookback_sec"] = lookback_sec
    if not window:
        return empty

    span = max(adv_interval, now - window[0].ts)
    expected = max(1.0, span / adv_interval)
    received = float(len(window))
    packet_ratio = min(1.0, received / expected)
    last = window[-1]
    time_since = now - last.ts

    gaps = [window[i].ts - window[i - 1].ts for i in range(1, len(window))]
    if time_since > 0:
        gaps.append(time_since)

    rssi_vals = [a.rssi for a in window if a.rssi is not None]
    rssi_times = [a.ts for a in window if a.rssi is not None]
    bats = [float(a.battery) for a in window if a.battery is not None]
    bat_times = [a.ts for a in window if a.battery is not None]

    rssi_now = rssi_vals[-1] if rssi_vals else 0.0
    rssi_mean = statistics.fmean(rssi_vals) if rssi_vals else 0.0
    rssi_min = min(rssi_vals) if rssi_vals else 0.0
    rssi_max = max(rssi_vals) if rssi_vals else 0.0
    rssi_std = statistics.pstdev(rssi_vals) if len(rssi_vals) > 1 else 0.0
    bat_now = bats[-1] if bats else -1.0
    tx = _as_float((last.extras or {}).get("txpower") or (last.extras or {}).get("tx_power")) or 0.0

    feats = {
        "rssi_now": rssi_now,
        "rssi_mean": rssi_mean,
        "rssi_min": rssi_min,
        "rssi_max": rssi_max,
        "rssi_std": rssi_std,
        "rssi_var": rssi_std ** 2,
        "rssi_slope": _lin_slope(rssi_times, rssi_vals) if len(rssi_vals) >= 2 else 0.0,
        "rssi_range": rssi_max - rssi_min,
        "rssi_degrade_sec": _degrade_duration(rssi_times, rssi_vals),
        "packets_received": received,
        "packets_expected": expected,
        "packet_ratio": packet_ratio,
        "missing_ratio": 1.0 - packet_ratio,
        "time_since_last": time_since,
        "longest_gap": max(gaps) if gaps else time_since,
        "mean_gap": statistics.fmean(gaps) if gaps else time_since,
        "gap_std": statistics.pstdev(gaps) if len(gaps) > 1 else 0.0,
        "battery_now": bat_now,
        "battery_slope": _lin_slope(bat_times, bats) if len(bats) >= 2 else 0.0,
        "battery_drop": (bats[0] - bats[-1]) if len(bats) >= 2 else 0.0,
        "tx_power": tx,
        "lookback_sec": lookback_sec,
    }
    return feats


def status_from_probability(prob_pct: float) -> str:
    if prob_pct >= LIKELY:
        return "Likely to Disconnect"
    if prob_pct >= AT_RISK:
        return "At Risk"
    return "Normal"


def heuristic_probability(feats: dict[str, float], disconnect_sec: float) -> float:
    """Fallback scorer used before a model is trained. Not RSSI-only."""
    miss = max(0.0, min(1.0, feats["missing_ratio"]))
    late = max(0.0, min(1.0, feats["time_since_last"] / max(disconnect_sec, 1.0)))
    gap = max(0.0, min(1.0, feats["longest_gap"] / max(disconnect_sec, 1.0)))
    var = max(0.0, min(1.0, feats["rssi_std"] / 12.0))
    slope = max(0.0, min(1.0, -feats["rssi_slope"] / 0.15))  # dB / s
    degrade = max(0.0, min(1.0, feats["rssi_degrade_sec"] / max(feats["lookback_sec"], 1.0)))
    batt = max(0.0, min(1.0, feats["battery_drop"] / 15.0))
    batt_low = 1.0 if 0 <= feats["battery_now"] < 20 else 0.0
    score = (
        0.32 * miss
        + 0.18 * late
        + 0.12 * gap
        + 0.12 * var
        + 0.12 * slope
        + 0.08 * degrade
        + 0.04 * batt
        + 0.02 * batt_low
    )
    return round(100.0 * max(0.0, min(1.0, score)), 1)


def _vector(feats: dict[str, float]) -> list[float]:
    return [float(feats.get(name, 0.0)) for name in FEATURE_NAMES]


def label_disconnect(
    ads: list[Advertisement],
    t: float,
    horizon_sec: float,
    disconnect_sec: float,
) -> tuple[int, float | None]:
    """1 if a ≥disconnect_sec gap starts within the prediction horizon.

    Returns -1 when the beacon is already down at t, or the log is censored
    so a negative label would be unreliable.
    """
    last_before = max((a.ts for a in ads if a.ts <= t), default=None)
    if last_before is None or (t - last_before) >= disconnect_sec:
        return -1, None
    log_end = ads[-1].ts
    future = [a.ts for a in ads if a.ts > t]
    points = [t] + future
    for i in range(1, len(points)):
        gap_start, gap_end = points[i - 1], points[i]
        if gap_end - gap_start >= disconnect_sec and gap_start <= t + horizon_sec:
            disconnect_at = gap_start + disconnect_sec
            return 1, max(0.0, disconnect_at - t)
    if log_end >= t + horizon_sec + disconnect_sec:
        return 0, None
    return -1, None


def observed_disconnect(
    ads: list[Advertisement],
    t: float,
    horizon_sec: float,
    disconnect_sec: float,
    now: float,
) -> tuple[int, float | None] | None:
    """Live label once the prediction horizon has elapsed. None = still waiting."""
    if now < t + horizon_sec:
        return None
    last = max((a.ts for a in ads if a.ts <= t), default=t)
    cursor = last
    for ts in [a.ts for a in ads if a.ts > t]:
        if ts - cursor >= disconnect_sec and cursor <= t + horizon_sec:
            return 1, max(0.0, cursor + disconnect_sec - t)
        cursor = ts
        if ts > t + horizon_sec + disconnect_sec:
            break
    if now - cursor >= disconnect_sec and cursor <= t + horizon_sec:
        return 1, max(0.0, cursor + disconnect_sec - t)
    return 0, None


class DisconnectionPredictor:
    def __init__(
        self,
        adv_interval: float = ADV_INTERVAL_SEC,
        disconnect_intervals: int = DISCONNECT_INTERVALS,
        lookback_sec: float = LOOKBACK_SEC,
        horizon_sec: float = HORIZON_SEC,
        ads_log: str = ADS_LOG,
        model_path: str = MODEL_FILE,
    ):
        self.adv_interval = adv_interval
        self.disconnect_sec = disconnect_intervals * adv_interval
        self.lookback_sec = lookback_sec
        self.horizon_sec = horizon_sec
        self.ads_log = ads_log
        self.model_path = model_path
        self.keep_sec = max(lookback_sec, horizon_sec) + 3600
        self.by_mac: dict[str, deque[Advertisement]] = defaultdict(lambda: deque(maxlen=8000))
        self.lock = threading.Lock()
        self.model = None
        self.feature_weights: dict[str, float] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.outcomes: deque[dict[str, Any]] = deque(maxlen=300)
        self.live_labels: list[dict[str, Any]] = []
        self.last_metrics: dict[str, Any] = {}
        self.suitable = False
        self.last_train_at = ""
        self.last_retrain_reason = "not trained yet"
        self.train_message = ""
        self.ads_written = 0
        self._needs_retrain = False
        self._retrain_lock = threading.Lock()
        self._last_retrain = 0.0
        self._load_model()
        self._load_labels()
        self._load_metrics()

    def ingest(self, ad: Advertisement, persist: bool = True) -> None:
        with self.lock:
            self.by_mac[ad.ble_addr].append(ad)
            self._trim(ad.ble_addr, ad.ts)
        if persist:
            with open(self.ads_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(ad.to_json()) + "\n")
            self.ads_written += 1

    def load_log(self, path: str | None = None) -> int:
        path = path or self.ads_log
        if not os.path.isfile(path):
            return 0
        n = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ad = Advertisement.from_json(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                self.by_mac[ad.ble_addr].append(ad)
                n += 1
        for mac in self.by_mac:
            ads = sorted(self.by_mac[mac], key=lambda a: a.ts)
            self.by_mac[mac] = deque(ads, maxlen=8000)
        self.ads_written = n
        return n

    def _trim(self, mac: str, now: float) -> None:
        q = self.by_mac[mac]
        cutoff = now - self.keep_sec
        while q and q[0].ts < cutoff:
            q.popleft()

    def features(self, mac: str, now: float | None = None) -> dict[str, float]:
        with self.lock:
            ads = list(self.by_mac.get(mac, ()))
        now = ads[-1].ts if now is None and ads else (now or time.time())
        return features_from_window(ads, now, self.lookback_sec, self.adv_interval)

    def predict_one(self, mac: str, now: float | None = None) -> dict[str, Any]:
        with self.lock:
            ads = list(self.by_mac.get(mac, ()))
        now = now or (ads[-1].ts if ads else time.time())
        feats = features_from_window(ads, now, self.lookback_sec, self.adv_interval)
        if self.model is not None:
            import numpy as np

            proba = float(self.model.predict_proba(np.array([_vector(feats)]))[0][1])
            prob_pct = round(100.0 * proba, 1)
            drivers = self._drivers(feats)
        else:
            prob_pct = heuristic_probability(feats, self.disconnect_sec)
            drivers = self._heuristic_drivers(feats)
        return {
            "ble_addr": mac,
            "timestamp": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
            "disconnect_probability": prob_pct,
            "predicted_status": status_from_probability(prob_pct),
            "prediction_horizon_sec": self.horizon_sec,
            "prediction_horizon": _horizon_label(self.horizon_sec),
            "key_features": drivers,
            "model": "sklearn" if self.model is not None else "heuristic",
            "features": {k: round(v, 4) if isinstance(v, float) else v for k, v in feats.items()},
        }

    def predict_all(self, now: float | None = None) -> list[dict[str, Any]]:
        with self.lock:
            macs = list(self.by_mac.keys())
        rows = [self.predict_one(mac, now) for mac in macs]
        rows.sort(key=lambda r: r["disconnect_probability"], reverse=True)
        return rows

    def _drivers(self, feats: dict[str, float], k: int = 5) -> list[dict[str, Any]]:
        if not self.feature_weights:
            return self._heuristic_drivers(feats)
        scored = []
        for name, weight in self.feature_weights.items():
            value = feats.get(name, 0.0)
            scored.append((abs(weight * value), name, value, weight))
        scored.sort(reverse=True)
        return [
            {"feature": name, "value": round(value, 4), "weight": round(weight, 4)}
            for _, name, value, weight in scored[:k]
        ]

    def _heuristic_drivers(self, feats: dict[str, float]) -> list[dict[str, Any]]:
        ranked = [
            ("missing_ratio", feats["missing_ratio"]),
            ("time_since_last", feats["time_since_last"]),
            ("rssi_std", feats["rssi_std"]),
            ("rssi_slope", feats["rssi_slope"]),
            ("longest_gap", feats["longest_gap"]),
            ("battery_drop", feats["battery_drop"]),
            ("rssi_degrade_sec", feats["rssi_degrade_sec"]),
        ]
        ranked.sort(key=lambda x: abs(x[1]), reverse=True)
        return [{"feature": n, "value": round(v, 4)} for n, v in ranked[:5]]

    def build_dataset(self) -> tuple[list[list[float]], list[int], list[float]]:
        X: list[list[float]] = []
        y: list[int] = []
        leads: list[float] = []
        with self.lock:
            groups = {mac: list(ads) for mac, ads in self.by_mac.items()}
        for ads in groups.values():
            ads = sorted(ads, key=lambda a: a.ts)
            if len(ads) < 10:
                continue
            t0 = ads[0].ts + self.lookback_sec
            t1 = ads[-1].ts - 1.0
            t = t0
            while t <= t1:
                label, lead = label_disconnect(ads, t, self.horizon_sec, self.disconnect_sec)
                if label >= 0:
                    feats = features_from_window(ads, t, self.lookback_sec, self.adv_interval)
                    X.append(_vector(feats))
                    y.append(label)
                    leads.append(-1.0 if lead is None else lead)
                t += SAMPLE_STEP_SEC
        return X, y, leads

    def train(self, test_frac: float = 0.3) -> dict[str, Any]:
        try:
            import numpy as np
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import Pipeline
            from sklearn.preprocessing import StandardScaler
        except ImportError as exc:
            raise ValueError(
                "scikit-learn is required to train. Install with: python -m pip install scikit-learn"
            ) from exc

        X, y, leads = self.build_dataset()
        for row in self.live_labels:
            X.append(list(row["x"]))
            y.append(int(row["y"]))
            leads.append(float(row.get("lead", -1.0)))
        if len(X) < 50 or len(set(y)) < 2:
            raise ValueError(
                f"Not enough labeled samples to train (n={len(X)}, positives={sum(y)}). "
                "Keep the dashboard running through dropouts, then the model will retry."
            )
        from sklearn.model_selection import train_test_split

        arr_x = np.array(X, dtype=float)
        arr_y = np.array(y, dtype=int)
        leads_n = np.array(leads, dtype=float)
        strat = arr_y if 0 < int(arr_y.sum()) < len(arr_y) else None
        x_train, x_test, y_train, y_test, _, lead_test = train_test_split(
            arr_x, arr_y, leads_n, test_size=test_frac, random_state=7, stratify=strat
        )

        pipe = Pipeline(
            [
                ("scale", StandardScaler()),
                (
                    "clf",
                    LogisticRegression(
                        max_iter=400,
                        class_weight="balanced",
                        solver="lbfgs",
                    ),
                ),
            ]
        )
        pipe.fit(x_train, y_train)
        self.model = pipe
        clf = pipe.named_steps["clf"]
        self.feature_weights = {
            name: float(w) for name, w in zip(FEATURE_NAMES, clf.coef_[0])
        }
        proba = pipe.predict_proba(x_test)[:, 1]
        pred = (proba >= 0.5).astype(int)
        metrics = evaluate_predictions(y_test, pred, proba, lead_test)
        metrics.update(
            {
                "n_samples": int(len(arr_x)),
                "n_positive": int(arr_y.sum()),
                "n_train": int(len(y_train)),
                "n_test": int(len(y_test)),
                "horizon_sec": self.horizon_sec,
                "lookback_sec": self.lookback_sec,
                "disconnect_sec": self.disconnect_sec,
                "feature_weights": {
                    k: round(v, 5)
                    for k, v in sorted(
                        self.feature_weights.items(), key=lambda kv: abs(kv[1]), reverse=True
                    )
                },
            }
        )
        self._save_model()
        with open(METRICS_FILE, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)
        return metrics

    def _save_model(self) -> None:
        with open(self.model_path, "wb") as f:
            pickle.dump(
                {
                    "model": self.model,
                    "feature_weights": self.feature_weights,
                    "lookback_sec": self.lookback_sec,
                    "horizon_sec": self.horizon_sec,
                    "disconnect_sec": self.disconnect_sec,
                    "adv_interval": self.adv_interval,
                    "feature_names": FEATURE_NAMES,
                },
                f,
            )

    def _load_model(self) -> None:
        if not os.path.isfile(self.model_path):
            return
        try:
            with open(self.model_path, "rb") as f:
                blob = pickle.load(f)
            self.model = blob["model"]
            self.feature_weights = blob.get("feature_weights") or {}
            self.lookback_sec = blob.get("lookback_sec", self.lookback_sec)
            self.horizon_sec = blob.get("horizon_sec", self.horizon_sec)
        except Exception:
            self.model = None

    def _load_metrics(self) -> None:
        if os.path.isfile(METRICS_FILE):
            try:
                with open(METRICS_FILE, encoding="utf-8") as f:
                    self.last_metrics = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.last_metrics = {}
        self._refresh_suitable()

    def _load_labels(self) -> None:
        if not os.path.isfile(LABELS_LOG):
            return
        with open(LABELS_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "x" in row and "y" in row:
                    self.live_labels.append(row)
        if os.path.isfile(OUTCOMES_LOG):
            with open(OUTCOMES_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.outcomes.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        self._refresh_suitable()

    def _refresh_suitable(self) -> None:
        recent = list(self.outcomes)[-SUITABLE_MIN_OUTCOMES:]
        if len(recent) < SUITABLE_MIN_OUTCOMES:
            self.suitable = False
            return
        correct = sum(1 for o in recent if o.get("correct"))
        acc = correct / len(recent)
        actual_pos = [o for o in recent if o.get("actual")]
        if actual_pos:
            recall = sum(1 for o in actual_pos if o.get("predicted")) / len(actual_pos)
        else:
            recall = 1.0
        self.suitable = acc >= SUITABLE_ACCURACY and recall >= 0.5 and self.model is not None

    def rolling_stats(self) -> dict[str, Any]:
        rows = list(self.outcomes)
        n = len(rows)
        correct = sum(1 for o in rows if o.get("correct"))
        recent = rows[-SUITABLE_MIN_OUTCOMES:]
        r_ok = sum(1 for o in recent if o.get("correct"))
        return {
            "resolved": n,
            "correct": correct,
            "incorrect": n - correct,
            "accuracy": None if n == 0 else round(correct / n, 3),
            "rolling_accuracy": None if not recent else round(r_ok / len(recent), 3),
            "rolling_n": len(recent),
        }

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        self._emit_predictions(now)
        self._resolve_outcomes(now)
        self._maybe_retrain(now)

    def _emit_predictions(self, now: float) -> None:
        with self.lock:
            macs = list(self.by_mac.keys())
        for mac in macs:
            if mac in self.pending:
                continue
            with self.lock:
                ads = list(self.by_mac.get(mac, ()))
            if len(ads) < 5:
                continue
            last = ads[-1].ts
            if now - last >= self.disconnect_sec:
                continue
            row = self.predict_one(mac, now)
            pred_pos = 1 if row["disconnect_probability"] >= AT_RISK else 0
            self.pending[mac] = {
                "ble_addr": mac,
                "t": now,
                "resolve_at": now + self.horizon_sec,
                "predicted": pred_pos,
                "probability": row["disconnect_probability"],
                "status": row["predicted_status"],
                "features": row["features"],
                "x": _vector(row["features"]),
                "horizon_sec": self.horizon_sec,
                "key_features": row["key_features"],
                "model": row["model"],
            }

    def _resolve_outcomes(self, now: float) -> None:
        done = []
        for mac, pend in list(self.pending.items()):
            with self.lock:
                ads = list(self.by_mac.get(mac, ()))
            result = observed_disconnect(
                ads, pend["t"], pend["horizon_sec"], self.disconnect_sec, now
            )
            if result is None:
                continue
            actual, lead = result
            predicted = int(pend["predicted"])
            correct = predicted == actual
            outcome = {
                "when": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                "ble_addr": mac,
                "predicted": predicted,
                "predicted_status": pend["status"],
                "probability": pend["probability"],
                "actual": actual,
                "actual_status": "Disconnected" if actual else "Stayed visible",
                "correct": correct,
                "lead_sec": None if lead is None else round(lead, 1),
                "horizon": _horizon_label(pend["horizon_sec"]),
            }
            self.outcomes.append(outcome)
            label_row = {
                "ts": pend["t"],
                "mac": mac,
                "x": pend["x"],
                "y": actual,
                "lead": -1.0 if lead is None else lead,
                "correct": correct,
            }
            self.live_labels.append(label_row)
            with open(OUTCOMES_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(outcome) + "\n")
            with open(LABELS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(label_row) + "\n")
            done.append(mac)
            if correct:
                if not self.suitable:
                    self.last_retrain_reason = "prediction correct — model kept"
            else:
                self.last_retrain_reason = "prediction wrong — training data updated"
                self._needs_retrain = True
        for mac in done:
            self.pending.pop(mac, None)
        self._refresh_suitable()

    def _maybe_retrain(self, now: float) -> None:
        if self.suitable:
            return
        if not getattr(self, "_needs_retrain", False):
            return
        if now - self._last_retrain < RETRAIN_COOLDOWN_SEC:
            return
        if not self._retrain_lock.acquire(blocking=False):
            return
        try:
            self._needs_retrain = False
            self._last_retrain = now
            try:
                metrics = self.train()
            except ValueError as exc:
                self.train_message = str(exc)
                return
            self.last_metrics = metrics
            self.last_train_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.train_message = "retrained after incorrect prediction"
            self._refresh_suitable()
            if self.suitable:
                self.last_retrain_reason = "suitable model found — keeping this behaviour"
        finally:
            self._retrain_lock.release()

    def dashboard_state(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        live = self.predict_all(now)
        stats = self.rolling_stats()
        pending = []
        for mac, pend in self.pending.items():
            pending.append({
                "ble_addr": mac,
                "predicted_status": pend["status"],
                "probability": pend["probability"],
                "resolves": datetime.fromtimestamp(pend["resolve_at"]).strftime("%H:%M:%S"),
                "key_features": pend.get("key_features") or [],
            })
        pending.sort(key=lambda r: r["probability"], reverse=True)
        return {
            "suitable": self.suitable,
            "model": "sklearn" if self.model is not None else "heuristic",
            "ads_collected": self.ads_written,
            "beacons": len(self.by_mac),
            "pending": pending,
            "pending_n": len(pending),
            "live": live[:40],
            "outcomes": list(self.outcomes)[-40:][::-1],
            "stats": stats,
            "metrics": self.last_metrics,
            "lookback": _horizon_label(self.lookback_sec).replace("next ", "last "),
            "horizon": _horizon_label(self.horizon_sec),
            "disconnect_sec": self.disconnect_sec,
            "last_train_at": self.last_train_at,
            "last_retrain_reason": self.last_retrain_reason,
            "train_message": self.train_message,
            "suitable_rule": (
                f"suitable when last {SUITABLE_MIN_OUTCOMES} outcomes are "
                f"≥{int(SUITABLE_ACCURACY*100)}% correct and disconnects are recalled"
            ),
        }



def evaluate_predictions(
    y_true,
    y_pred,
    y_proba,
    lead_times,
) -> dict[str, Any]:
    try:
        from sklearn.metrics import (
            average_precision_score,
            confusion_matrix,
            f1_score,
            precision_score,
            recall_score,
            roc_auc_score,
        )
    except ImportError:
        return {"error": "scikit-learn not installed"}

    cm = confusion_matrix(y_true, y_pred, labels=[0, 1])
    tn, fp, fn, tp = (int(x) for x in cm.ravel())
    prec = precision_score(y_true, y_pred, zero_division=0)
    rec = recall_score(y_true, y_pred, zero_division=0)
    f1 = f1_score(y_true, y_pred, zero_division=0)
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    fnr = fn / (fn + tp) if (fn + tp) else 0.0
    try:
        roc = roc_auc_score(y_true, y_proba)
    except ValueError:
        roc = None
    try:
        pr = average_precision_score(y_true, y_proba)
    except ValueError:
        pr = None
    pos_leads = [float(t) for t, yt, yp in zip(lead_times, y_true, y_pred) if yt == 1 and yp == 1 and t >= 0]
    return {
        "precision": round(float(prec), 4),
        "recall": round(float(rec), 4),
        "f1": round(float(f1), 4),
        "false_positive_rate": round(fpr, 4),
        "false_negative_rate": round(fnr, 4),
        "roc_auc": None if roc is None else round(float(roc), 4),
        "pr_auc": None if pr is None else round(float(pr), 4),
        "mean_lead_time_sec": None if not pos_leads else round(sum(pos_leads) / len(pos_leads), 1),
        "median_lead_time_sec": None if not pos_leads else round(float(statistics.median(pos_leads)), 1),
        "confusion": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
    }


def _horizon_label(sec: float) -> str:
    mins = int(round(sec / 60.0))
    return f"next {mins} minutes"


def _cfc_ids(raw: str) -> list[str]:
    return [p for p in raw.upper().replace(" ", "").replace(":", "").replace("'", "").split(",") if p]


def collect_loop(predictor: DisconnectionPredictor, cfc: list[str], do_predict: bool) -> None:
    import paho.mqtt.client as mqtt

    user, password = load_credentials()

    def on_connect(client, userdata, flags, reason_code, properties):
        print("MQTT connected, subscribing", TOPIC)
        client.subscribe(TOPIC)

    def on_message(client, userdata, msg):
        body = msg.payload.decode(errors="replace")
        if cfc and not any(i in body.upper().replace(":", "") for i in cfc):
            return
        ads = ingest_mqtt_payload(body)
        for ad in ads:
            predictor.ingest(ad)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(user, password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, MQTT_PORT)
    print(f"Collecting E9 ads to {predictor.ads_log}")
    print(f"lookback={predictor.lookback_sec}s horizon={predictor.horizon_sec}s "
          f"disconnect={predictor.disconnect_sec}s")
    client.loop_start()
    try:
        while True:
            time.sleep(30)
            if do_predict:
                rows = predictor.predict_all()
                risky = [r for r in rows if r["predicted_status"] != "Normal"]
                print(datetime.now().strftime("%H:%M:%S"),
                      f"beacons={len(rows)} at_risk={len(risky)}")
                for row in risky[:8]:
                    print(" ", row["ble_addr"], f"{row['disconnect_probability']}%",
                          row["predicted_status"],
                          ",".join(d["feature"] for d in row["key_features"][:3]))
            else:
                with predictor.lock:
                    n = sum(len(q) for q in predictor.by_mac.values())
                print(datetime.now().strftime("%H:%M:%S"), f"ads_in_memory={n}")
    except KeyboardInterrupt:
        print("stopping")
    finally:
        client.loop_stop()
        client.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E9 BLE disconnection predictor")
    parser.add_argument("command", choices=("collect", "train", "predict"))
    parser.add_argument("--cfc", default=os.environ.get("E9_CFC") or load_config().esn)
    parser.add_argument("--lookback", type=int, default=int(LOOKBACK_SEC),
                        help="history window seconds (60–300)")
    parser.add_argument("--horizon", type=int, default=int(HORIZON_SEC),
                        help="prediction window seconds (300, 600, or 1800)")
    parser.add_argument("--n-intervals", type=int, default=DISCONNECT_INTERVALS,
                        help="disconnect = N × 3s with no advertisement")
    parser.add_argument("--predict", action="store_true",
                        help="with collect: print live risk every 30s")
    parser.add_argument("--ads", default=ADS_LOG)
    parser.add_argument("--model", default=MODEL_FILE)
    args = parser.parse_args(argv)

    lookback = min(300, max(60, args.lookback))
    horizon = args.horizon
    predictor = DisconnectionPredictor(
        lookback_sec=lookback,
        horizon_sec=horizon,
        disconnect_intervals=args.n_intervals,
        ads_log=args.ads,
        model_path=args.model,
    )

    if args.command == "collect":
        collect_loop(predictor, _cfc_ids(args.cfc), args.predict)
        return 0

    n = predictor.load_log(args.ads)
    print(f"loaded {n} advertisements from {args.ads}")
    if args.command == "train":
        try:
            metrics = predictor.train()
        except ValueError as exc:
            print(exc)
            return 1
        print(json.dumps(metrics, indent=2))
        print(f"model saved to {args.model}")
        print(f"metrics saved to {METRICS_FILE}")
        return 0

    if not predictor.by_mac:
        print("No advertisements loaded. Run: python disconnection_predictor.py collect")
        return 1
    for row in predictor.predict_all():
        print(
            f"{row['ble_addr']}  {row['disconnect_probability']:5.1f}%  "
            f"{row['predicted_status']:22}  {row['prediction_horizon']}  "
            f"{row['key_features'][0]['feature'] if row['key_features'] else '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
