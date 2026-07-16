import json
import os
import time
import threading
import warnings
import datetime
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
import re
from typing import Any, Dict, Optional, List

import paho.mqtt.client as mqtt

from debug_server import LaresState, start_debug_server, set_command_handler
from pwm_controller import PWMController

CONFIG_PATH = "/data/vtherm.json"
RUNTIME_PATH = "/data/vtherm_runtime.json"
EVENTS_PATH = "/data/e_therm_events.jsonl"
APP_VERSION = "2.6.224"
print(f"[BOOT] e-Therm code version {APP_VERSION}")
_OPTIONS_WARNED = False

# Keep logs clean in HA while we intentionally run callback API v1 for compatibility.
warnings.filterwarnings(
    "ignore",
    message="Callback API version 1 is deprecated, update to latest version",
    category=DeprecationWarning,
)

DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_options() -> Dict[str, Any]:
    global _OPTIONS_WARNED
    path = "/data/options.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        if not _OPTIONS_WARNED:
            print(f"[WARN] options.json is not a dict: {type(data).__name__}")
            _OPTIONS_WARNED = True
        return {}
    except Exception as e:
        if not _OPTIONS_WARNED:
            print(f"[WARN] cannot read {path}: {e}")
            _OPTIONS_WARNED = True
        return {}


def load_config() -> Dict[str, Any]:
    return _load_json(CONFIG_PATH)


def save_config(cfg: Dict[str, Any]) -> None:
    _save_json(CONFIG_PATH, cfg)


def load_runtime() -> Dict[str, Any]:
    return _load_json(RUNTIME_PATH)


def save_runtime(rt: Dict[str, Any]) -> None:
    _save_json(RUNTIME_PATH, rt)


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip().replace(",", ".")
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _as_int(x: Any) -> Optional[int]:
    try:
        v = _as_float(x)
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _parse_iso_datetime(value: Any) -> Optional[datetime.datetime]:
    try:
        s = str(value or "").strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _dict_get_path(d: Any, path: List[str]) -> Any:
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _get_any(d: Dict[str, Any], *candidates: Any) -> Any:
    """Try multiple candidates.

    Candidate can be:
    - a string key
    - a list/tuple path for nested dicts
    """
    for c in candidates:
        try:
            if isinstance(c, str):
                if c in d:
                    return d.get(c)
                continue
            if isinstance(c, (list, tuple)):
                v = _dict_get_path(d, list(c))
                if v is not None:
                    return v
        except Exception:
            continue
    return None

def _topic_safe_name(name: Any) -> str:
    try:
        s = str(name or "").strip()
        if not s:
            return "unknown"
        s = s.replace("/", "_").replace("\\", "_")
        s = "_".join(s.split())
        return s
    except Exception:
        return "unknown"


def _entity_safe_name(name: Any, fallback: str = "unknown") -> str:
    try:
        s = str(name or "").strip().lower()
        out = []
        prev_us = False
        for ch in s:
            if ch.isalnum():
                out.append(ch)
                prev_us = False
            else:
                if not prev_us:
                    out.append("_")
                    prev_us = True
        slug = "".join(out).strip("_")
        return slug or str(fallback or "unknown")
    except Exception:
        return str(fallback or "unknown")


class ThermEngine:
    def __init__(self, state: LaresState, opts: Dict[str, Any]):
        self.state = state
        self.opts = opts
        self.lock = threading.Lock()
        self._mqtt_manage_lock = threading.Lock()
        self._mqtt_reconnecting = False

        self.cfg = load_config() or {}
        self.runtime = load_runtime() or {}

        self.desired: Dict[str, Dict[str, Any]] = {}
        d = self.runtime.get("desired")
        if isinstance(d, dict):
            self.desired = d

        self.therm_static: Dict[str, Dict[str, Any]] = {}
        s = self.runtime.get("therm_static")
        if isinstance(s, dict):
            self.therm_static = s

        self.source_prefix = str(opts.get("source_prefix", "e-safe")).strip().rstrip("/")
        self.out_prefix = str(opts.get("out_prefix", "e-therm")).strip().rstrip("/")

        # MQTT
        self.mqtt = self._create_mqtt_client()
        self._mqtt_connected = False
        self._pending_discovery_cleanup: List[str] = []
        self._last_mqtt_any_ts = 0.0
        self._last_source_ts = 0.0
        self._ever_got_source = False
        self._last_reconnect_attempt_ts = 0.0
        self._reconnect_backoff_sec = 5.0
        self._last_reconnect_reason = ""
        self._last_mqtt_error = ""
        self._last_ha_poll_ts = 0.0
        self._last_ha_sensor_poll_ts = 0.0
        self._last_ha_plain_sensor_poll_ts = 0.0
        self._last_ha_warn_ts = 0.0
        self._last_discovery_publish_ts = 0.0
        self._last_control_runtime_save_ts = 0.0

        # realtime cache per vtherm id
        self.rt: Dict[str, Dict[str, Any]] = {}
        rt_cache = self.runtime.get("rt_cache")
        if isinstance(rt_cache, dict):
            self.rt = rt_cache
        self.auto_control_enabled = bool(opts.get("auto_control_enabled", False))
        self.auto_override_sec = int(opts.get("auto_override_sec", 300) or 300)
        self.pwm_kp = float(opts.get("pwm_kp", 10.0) or 10.0)
        self.pwm_ki = float(opts.get("pwm_ki", 0.1) or 0.1)
        self.pwm_windup = float(opts.get("pwm_windup", 100.0) or 100.0)
        self.pwm_deadband = float(opts.get("pwm_deadband", 0.2) or 0.2)
        self.pwm_deadband_on = float(opts.get("pwm_deadband_on", self.pwm_deadband) or self.pwm_deadband)
        self.pwm_deadband_off = float(opts.get("pwm_deadband_off", self.pwm_deadband) or self.pwm_deadband)
        pwm_full_error_opt = opts.get("pwm_full_error", 1.5)
        self.pwm_full_error = float(1.5 if pwm_full_error_opt in (None, "") else pwm_full_error_opt)
        pwm_min_active_opt = opts.get("pwm_min_active", 15)
        self.pwm_min_active = int(max(0, min(100, int(15 if pwm_min_active_opt in (None, "") else pwm_min_active_opt))))
        self.pwm_min_to_med = int(opts.get("pwm_min_to_med", 34) or 34)
        self.pwm_med_to_max = int(opts.get("pwm_med_to_max", 67) or 67)
        self.real_fan_min_hold_sec = int(opts.get("real_fan_min_hold_sec", 0) or 0)
        self.real_fan_strict_mirror = bool(opts.get("real_fan_strict_mirror", True))
        self._pwm: Dict[str, PWMController] = {}
        self._manual_override_until: Dict[str, float] = {}
        self._manual_valve_until: Dict[str, float] = {}
        self._manual_valve_state: Dict[str, Dict[str, bool]] = {}
        self._real_target_last: Dict[str, Any] = {}
        self._real_switch_skip_warned: set[str] = set()
        self._demand_latch: Dict[str, bool] = {}
        self._real_therm_adapt: Dict[str, Dict[str, Any]] = {}
        self._ha_bridge_cmd_last: Dict[str, float] = {}
        self._ha_bridge_mode_hold: Dict[str, Dict[str, Any]] = {}
        self._ha_bridge_setpoint_hold: Dict[str, float] = {}
        self._control_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._computherm_thread: Optional[threading.Thread] = None
        self._computherm_stop = False
        self._computherm_last_error = ""
        self._computherm_last_poll_ts = 0.0

        # event log (for /logs UI)
        self._events_lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._events_file_count = 0
        self._last_temp_log: Dict[str, Dict[str, Any]] = {}
        self._event_seq = 0
        self._pending_acks: Dict[str, Dict[str, Any]] = {}
        self._load_events()
        try:
            self._log_event(
                origin="system",
                tid=None,
                name=None,
                source_num=None,
                category="system",
                field="startup",
                msg=f"ThermEngine init v{APP_VERSION}",
            )
        except Exception:
            pass

        # log config
        self.log_level = str(opts.get("log_level", "MIN") or "MIN").strip().upper()
        if self.log_level not in ("MIN", "NORMAL", "DEBUG"):
            self.log_level = "MIN"
        self.log_auto_pwm_step = int(opts.get("log_auto_pwm_step", 5) or 5)
        self.log_auto_min_sec = int(opts.get("log_auto_min_sec", 60) or 60)
        self.log_temp_delta = float(opts.get("log_temp_delta", 0.5) or 0.5)
        self.log_temp_max_sec = int(opts.get("log_temp_max_sec", 600) or 600)
        self.log_rh_delta = float(opts.get("log_rh_delta", 10.0) or 10.0)
        self.log_rh_max_sec = int(opts.get("log_rh_max_sec", 600) or 600)
        self.log_ack_timeout_sec = int(opts.get("log_ack_timeout_sec", 20) or 20)
        self.log_file_max_kb = int(opts.get("log_file_max_kb", 2048) or 2048)

    def _computherm_options(self) -> Dict[str, Any]:
        o = load_options()
        enabled = bool(o.get("computherm_enabled", False))
        dashboards = [
            {"id": "ct", "name": "CT", "url": str(o.get("computherm_dashboard_ct_url") or "").strip()},
            {"id": "subct_1", "name": "SUBCT 1", "url": str(o.get("computherm_dashboard_subct_1_url") or "").strip()},
            {"id": "subct_2", "name": "SUBCT 2", "url": str(o.get("computherm_dashboard_subct_2_url") or "").strip()},
        ]
        return {
            "enabled": enabled,
            "login_url": str(o.get("computherm_login_url") or "").strip(),
            "username": str(o.get("computherm_username") or "").strip(),
            "password": str(o.get("computherm_password") or ""),
            "poll_interval_sec": max(60, int(o.get("computherm_poll_interval_sec", 300) or 300)),
            "dashboards": [d for d in dashboards if d.get("url")],
            "username_field": "ctl00$cph_body$txt_login_email",
            "password_field": "ctl00$cph_body$txt_login_password",
            "login_button_field": "ctl00$cph_body$btn_login",
            "login_button_value": "Accedi",
            "refresh_button_name": "ctl00$cph_body$ibtn_read_io_1",
        }

    def _computherm_state_meta(self, status: str, **extra: Any) -> None:
        meta = {
            "enabled": bool((self._computherm_options() or {}).get("enabled")),
            "status": status,
            "last_poll_ts": float(self._computherm_last_poll_ts or 0.0),
            "last_error": str(self._computherm_last_error or ""),
        }
        meta.update(extra)
        try:
            self.state.set_meta("computherm", meta)
        except Exception:
            pass

    def _computherm_http_client(self):
        cookies = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        return opener, headers

    def _computherm_get(self, opener, headers: Dict[str, str], url: str, timeout: int = 30) -> str:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")

    def _computherm_post_form(self, opener, headers: Dict[str, str], url: str, data: Dict[str, str], timeout: int = 45) -> str:
        h = dict(headers)
        h["Content-Type"] = "application/x-www-form-urlencoded"
        raw = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=raw, headers=h, method="POST")
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace")

    def _computherm_form_payload(self, html: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for m in re.finditer(r"<input\b[^>]*>", html, flags=re.I | re.S):
            tag = m.group(0)
            nm = re.search(r'\bname=["\']([^"\']+)["\']', tag, flags=re.I)
            if not nm:
                continue
            typ = re.search(r'\btype=["\']([^"\']+)["\']', tag, flags=re.I)
            t = (typ.group(1).lower() if typ else "")
            if t in ("submit", "button", "image", "file"):
                continue
            val = re.search(r'\bvalue=["\']([^"\']*)["\']', tag, flags=re.I | re.S)
            out[nm.group(1)] = val.group(1) if val else ""
        return out

    def _computherm_js_array(self, html: str, name: str) -> List[Any]:
        m = re.search(rf"var\s+{re.escape(name)}\s*=\s*new\s+Array\s*\((.*?)\);", html, flags=re.S)
        if not m:
            return []
        raw = m.group(1).strip()
        if raw == "null":
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _computherm_sensors(self, html: str) -> List[Dict[str, Any]]:
        for name in ("CSSensors", "CSProbes"):
            arr = self._computherm_js_array(html, name)
            if arr:
                return [x for x in arr if isinstance(x, dict)]
        return []

    def _computherm_leds(self, html: str) -> List[Dict[str, Any]]:
        arr = self._computherm_js_array(html, "CSLeds")
        return [x for x in arr if isinstance(x, dict)]

    def _computherm_items(self, html: str, name: str) -> List[Dict[str, Any]]:
        arr = self._computherm_js_array(html, name)
        return [x for x in arr if isinstance(x, dict)]

    def _computherm_login(self, opener, headers: Dict[str, str], cfg: Dict[str, Any]) -> None:
        login_url = str(cfg.get("login_url") or "").strip()
        login_html = self._computherm_get(opener, headers, login_url)
        payload = self._computherm_form_payload(login_html)
        payload[str(cfg.get("username_field"))] = str(cfg.get("username") or "")
        payload[str(cfg.get("password_field"))] = str(cfg.get("password") or "")
        btn = str(cfg.get("login_button_field") or "").strip()
        if btn:
            payload[btn] = str(cfg.get("login_button_value") or "Accedi")
        action = re.search(r"<form\b[^>]*\baction=[\"']([^\"']+)[\"']", login_html, flags=re.I | re.S)
        post_url = urllib.parse.urljoin(login_url, action.group(1)) if action else login_url
        self._computherm_post_form(opener, headers, post_url, payload, timeout=30)

    def _computherm_slug(self, value: Any) -> str:
        s = _entity_safe_name(value, "sensor")
        return s or "sensor"

    def _computherm_publish_sensor(self, dash_id: str, dash_name: str, probe: Dict[str, Any]) -> None:
        try:
            if not self._mqtt_connected:
                return
            label = str(probe.get("Label") or "").replace("\r", " ").replace("\n", " ").strip()
            sid = str(probe.get("SinId") or self._computherm_slug(label))
            uid = f"e_therm_computherm_{self._computherm_slug(dash_id)}_{sid}"
            state_topic = f"{self.out_prefix}/computherm/{self._computherm_slug(dash_id)}/{sid}/state"
            cfg_topic = f"homeassistant/sensor/{uid}/config"
            unit = str(probe.get("UDM") or "").replace("Â°", "°").replace("�", "°")
            if unit == "°":
                unit = "°C"
            dev_class = "temperature" if unit == "°C" else None
            payload = {
                "name": f"Computherm {dash_name} {label}",
                "unique_id": uid,
                "state_topic": state_topic,
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": {
                    "identifiers": [f"e_therm_computherm_{self._computherm_slug(dash_id)}"],
                    "name": f"Computherm {dash_name}",
                    "manufacturer": "Computherm",
                },
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if dev_class:
                payload["device_class"] = dev_class
                payload["state_class"] = "measurement"
            self.mqtt.publish(cfg_topic, json.dumps(payload, ensure_ascii=False), retain=True)
            self.mqtt.publish(state_topic, str(probe.get("Value")), retain=True)
        except Exception:
            pass

    def _computherm_publish_binary(self, dash_id: str, dash_name: str, led: Dict[str, Any]) -> None:
        try:
            if not self._mqtt_connected:
                return
            label = str(led.get("Label") or "").replace("\r", " ").replace("\n", " ").strip()
            sid = str(led.get("SinId") or self._computherm_slug(label))
            uid = f"e_therm_computherm_{self._computherm_slug(dash_id)}_led_{sid}"
            state_topic = f"{self.out_prefix}/computherm/{self._computherm_slug(dash_id)}/led/{sid}/state"
            cfg_topic = f"homeassistant/binary_sensor/{uid}/config"
            payload = {
                "name": f"Computherm {dash_name} {label}",
                "unique_id": uid,
                "state_topic": state_topic,
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": {
                    "identifiers": [f"e_therm_computherm_{self._computherm_slug(dash_id)}"],
                    "name": f"Computherm {dash_name}",
                    "manufacturer": "Computherm",
                },
            }
            self.mqtt.publish(cfg_topic, json.dumps(payload, ensure_ascii=False), retain=True)
            self.mqtt.publish(state_topic, "ON" if bool(led.get("State")) else "OFF", retain=True)
        except Exception:
            pass

    def _computherm_publish_number(self, dash_id: str, dash_name: str, kind: str, sid: str, label: str, value: Any, unit: str = "") -> None:
        try:
            if not self._mqtt_connected:
                return
            kind_slug = self._computherm_slug(kind)
            uid = f"e_therm_computherm_{self._computherm_slug(dash_id)}_{kind_slug}_{sid}"
            state_topic = f"{self.out_prefix}/computherm/{self._computherm_slug(dash_id)}/{kind_slug}/{sid}/state"
            cfg_topic = f"homeassistant/sensor/{uid}/config"
            payload = {
                "name": f"Computherm {dash_name} {label}",
                "unique_id": uid,
                "state_topic": state_topic,
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": {
                    "identifiers": [f"e_therm_computherm_{self._computherm_slug(dash_id)}"],
                    "name": f"Computherm {dash_name}",
                    "manufacturer": "Computherm",
                },
            }
            if unit:
                payload["unit_of_measurement"] = unit
            self.mqtt.publish(cfg_topic, json.dumps(payload, ensure_ascii=False), retain=True)
            self.mqtt.publish(state_topic, str(value), retain=True)
        except Exception:
            pass

    def _computherm_publish_bool(self, dash_id: str, dash_name: str, kind: str, sid: str, label: str, on: bool) -> None:
        try:
            if not self._mqtt_connected:
                return
            kind_slug = self._computherm_slug(kind)
            uid = f"e_therm_computherm_{self._computherm_slug(dash_id)}_{kind_slug}_{sid}"
            state_topic = f"{self.out_prefix}/computherm/{self._computherm_slug(dash_id)}/{kind_slug}/{sid}/state"
            cfg_topic = f"homeassistant/binary_sensor/{uid}/config"
            payload = {
                "name": f"Computherm {dash_name} {label}",
                "unique_id": uid,
                "state_topic": state_topic,
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": {
                    "identifiers": [f"e_therm_computherm_{self._computherm_slug(dash_id)}"],
                    "name": f"Computherm {dash_name}",
                    "manufacturer": "Computherm",
                },
            }
            self.mqtt.publish(cfg_topic, json.dumps(payload, ensure_ascii=False), retain=True)
            self.mqtt.publish(state_topic, "ON" if bool(on) else "OFF", retain=True)
        except Exception:
            pass

    def _computherm_poll_once(self) -> Dict[str, Any]:
        cfg = self._computherm_options()
        if not cfg.get("enabled"):
            self._computherm_state_meta("disabled")
            return {"ok": False, "status": "disabled", "error": "computherm_disabled", "sensor_count": 0}
        if not cfg.get("login_url") or not cfg.get("username") or not cfg.get("password") or not cfg.get("dashboards"):
            self._computherm_last_error = "missing_config"
            self._computherm_state_meta("missing_config")
            return {"ok": False, "status": "missing_config", "error": "missing_config", "sensor_count": 0}
        opener, headers = self._computherm_http_client()
        self._computherm_login(opener, headers, cfg)
        total = 0
        changed = []
        now = time.time()
        btn = str(cfg.get("refresh_button_name") or "ctl00$cph_body$ibtn_read_io_1")
        for dash in cfg.get("dashboards") or []:
            did = str(dash.get("id") or "").strip() or "dashboard"
            dname = str(dash.get("name") or did).strip()
            url = str(dash.get("url") or "").strip()
            html = self._computherm_get(opener, headers, url)
            payload = self._computherm_form_payload(html)
            payload[f"{btn}.x"] = "32"
            payload[f"{btn}.y"] = "32"
            html = self._computherm_post_form(opener, headers, url, payload)
            sensors = self._computherm_sensors(html)
            leds = self._computherm_leds(html)
            selectors = self._computherm_items(html, "CSSelectors")
            pumps = self._computherm_items(html, "CSPumps")
            valves = self._computherm_items(html, "CSValves")
            fans = self._computherm_items(html, "CSFans")
            dials = self._computherm_items(html, "CSDials")
            total += len(sensors)
            for p in sensors:
                label = str(p.get("Label") or "").replace("\r", " ").replace("\n", " ").strip()
                sid = str(p.get("SinId") or self._computherm_slug(label))
                ent_id = f"{self._computherm_slug(did)}_{sid}"
                unit = str(p.get("UDM") or "").replace("Â°", "°").replace("�", "°")
                ent = self.state._upsert("computherm_sensor", ent_id, {
                    "name": f"{dname} {label}",
                    "static": {
                        "dashboard_id": did,
                        "dashboard_name": dname,
                        "sin_id": sid,
                        "label": label,
                        "unit": unit,
                        "corr": p.get("Corr"),
                    },
                    "realtime": {
                        "value": p.get("Value"),
                        "unit": unit,
                        "last_poll_ts": now,
                    },
                }, now)
                if ent:
                    changed.append(ent)
                self._computherm_publish_sensor(did, dname, p)
            total += len(leds)
            for led in leds:
                label = str(led.get("Label") or "").replace("\r", " ").replace("\n", " ").strip()
                sid = str(led.get("SinId") or self._computherm_slug(label))
                ent_id = f"{self._computherm_slug(did)}_led_{sid}"
                ent = self.state._upsert("computherm_led", ent_id, {
                    "name": f"{dname} {label}",
                    "static": {
                        "dashboard_id": did,
                        "dashboard_name": dname,
                        "sin_id": sid,
                        "label": label,
                        "color_on": led.get("ColorOn"),
                        "color_off": led.get("ColorOff"),
                    },
                    "realtime": {
                        "state": "ON" if bool(led.get("State")) else "OFF",
                        "value": 1 if bool(led.get("State")) else 0,
                        "last_poll_ts": now,
                    },
                }, now)
                if ent:
                    changed.append(ent)
                self._computherm_publish_binary(did, dname, led)
            for item in selectors:
                label = str(item.get("Label") or "").replace("\r", " ").replace("\n", " ").strip()
                sid = str(item.get("SinId") or self._computherm_slug(label))
                state_on = bool(item.get("State"))
                total += 1
                ent = self.state._upsert("computherm_selector", f"{self._computherm_slug(did)}_selector_{sid}", {
                    "name": f"{dname} {label}",
                    "static": {"dashboard_id": did, "dashboard_name": dname, "sin_id": sid, "label": label, "read_only": bool(item.get("ReadOnly"))},
                    "realtime": {"state": "ON" if state_on else "OFF", "value": 1 if state_on else 0, "last_poll_ts": now},
                }, now)
                if ent:
                    changed.append(ent)
                self._computherm_publish_bool(did, dname, "selector", sid, label, state_on)
            for item in pumps:
                label = str(item.get("Label") or item.get("Name") or item.get("Text") or "").replace("\r", " ").replace("\n", " ").strip()
                sid = str(item.get("SinId") or self._computherm_slug(label))
                state_on = any(bool(item.get(k)) for k in ("C1", "F1", "B1", "C2", "F2", "B2"))
                total += 1
                ent = self.state._upsert("computherm_pump", f"{self._computherm_slug(did)}_pump_{sid}", {
                    "name": f"{dname} {label}",
                    "static": {"dashboard_id": did, "dashboard_name": dname, "sin_id": sid, "label": label, "type": item.get("Type"), "direction": item.get("Direction")},
                    "realtime": {
                        "state": "ON" if state_on else "OFF",
                        "value": 1 if state_on else 0,
                        "C1": bool(item.get("C1")),
                        "F1": bool(item.get("F1")),
                        "B1": bool(item.get("B1")),
                        "C2": bool(item.get("C2")),
                        "F2": bool(item.get("F2")),
                        "B2": bool(item.get("B2")),
                        "last_poll_ts": now,
                    },
                }, now)
                if ent:
                    changed.append(ent)
                self._computherm_publish_bool(did, dname, "pump", sid, label, state_on)
            for item in fans:
                label = str(item.get("Label") or item.get("Name") or item.get("Text") or "").replace("\r", " ").replace("\n", " ").strip()
                sid = str(item.get("SinId") or self._computherm_slug(label))
                state_on = bool(item.get("C1")) or bool(item.get("F1"))
                total += 1
                ent = self.state._upsert("computherm_fan", f"{self._computherm_slug(did)}_fan_{sid}", {
                    "name": f"{dname} {label}",
                    "static": {"dashboard_id": did, "dashboard_name": dname, "sin_id": sid, "label": label},
                    "realtime": {"state": "ON" if state_on else "OFF", "C1": bool(item.get("C1")), "F1": bool(item.get("F1")), "B1": bool(item.get("B1")), "last_poll_ts": now},
                }, now)
                if ent:
                    changed.append(ent)
                self._computherm_publish_bool(did, dname, "fan", sid, label, state_on)
            for item in valves:
                label = str(item.get("Label") or item.get("Name") or item.get("Text") or "").replace("\r", " ").replace("\n", " ").strip()
                sid = str(item.get("SinId") or self._computherm_slug(label))
                val = item.get("Value")
                total += 1
                ent = self.state._upsert("computherm_valve", f"{self._computherm_slug(did)}_valve_{sid}", {
                    "name": f"{dname} {label}",
                    "static": {"dashboard_id": did, "dashboard_name": dname, "sin_id": sid, "label": label},
                    "realtime": {"value": val, "open": item.get("Open"), "close": item.get("Close"), "last_poll_ts": now},
                }, now)
                if ent:
                    changed.append(ent)
                self._computherm_publish_number(did, dname, "valve", sid, label, val)
            for item in dials:
                label = str(item.get("Name") or item.get("Label") or "").replace("\r", " ").replace("\n", " ").strip()
                sid = str(item.get("SinId") or self._computherm_slug(label))
                val = item.get("Value")
                total += 1
                ent = self.state._upsert("computherm_dial", f"{self._computherm_slug(did)}_dial_{sid}", {
                    "name": f"{dname} {label}",
                    "static": {"dashboard_id": did, "dashboard_name": dname, "sin_id": sid, "label": label, "min": item.get("Min"), "max": item.get("Max"), "read_only": bool(item.get("ReadOnly"))},
                    "realtime": {"value": val, "last_poll_ts": now},
                }, now)
                if ent:
                    changed.append(ent)
                self._computherm_publish_number(did, dname, "dial", sid, label, val)
        self._computherm_last_poll_ts = now
        self._computherm_last_error = ""
        self._computherm_state_meta("ok", sensor_count=total)
        if changed:
            try:
                self.state._publish_event({"type": "update", "meta": {"last_update": now}, "entities": changed})
            except Exception:
                pass
        return {"ok": True, "status": "ok", "sensor_count": total, "changed_count": len(changed)}

    def start_computherm(self) -> None:
        if self._computherm_thread and self._computherm_thread.is_alive():
            return
        self._computherm_thread = threading.Thread(target=self._computherm_loop, name="computherm_loop", daemon=True)
        self._computherm_thread.start()

    def _computherm_loop(self) -> None:
        while True:
            try:
                cfg = self._computherm_options()
                interval = float(cfg.get("poll_interval_sec") or 300)
                if cfg.get("enabled"):
                    self._computherm_poll_once()
                else:
                    self._computherm_state_meta("disabled")
                    interval = 60.0
            except Exception as exc:
                self._computherm_last_error = str(exc)
                self._computherm_state_meta("error")
                interval = 120.0
            time.sleep(max(60.0, float(interval or 300)))

    def _opt_seconds(self, key: str, default: float, minimum: float = 1.0) -> float:
        try:
            value = float(self.opts.get(key, default) or default)
        except Exception:
            value = float(default)
        return max(float(minimum), float(value))

    def _persist_rt_cache(self) -> None:
        try:
            with self.lock:
                self.runtime["rt_cache"] = self.rt
                save_runtime(self.runtime)
        except Exception:
            pass

    def _log_enabled(self, level: str) -> bool:
        want = str(level or "MIN").upper()
        cur = str(self.log_level or "MIN").upper()
        order = {"MIN": 0, "NORMAL": 1, "DEBUG": 2}
        return order.get(cur, 0) >= order.get(want, 0)

    def _ack_key(self, tid: str, field: str) -> str:
        return f"{str(tid)}:{str(field)}"

    def _register_ack(self, *, tid: str, field: str, origin: str, expected: Any) -> None:
        try:
            self._pending_acks[self._ack_key(tid, field)] = {
                "ts": time.time(),
                "origin": str(origin or ""),
                "expected": expected,
            }
        except Exception:
            pass

    def _maybe_ack(self, *, tid: str, field: str, new_value: Any, name: str, source_num: int) -> None:
        try:
            k = self._ack_key(tid, field)
            ack = self._pending_acks.get(k)
            if not ack:
                return
            ts0 = float(ack.get("ts") or 0.0)
            if not ts0:
                self._pending_acks.pop(k, None)
                return
            if (time.time() - ts0) > float(self.log_ack_timeout_sec):
                # timeout
                self._pending_acks.pop(k, None)
                if self._log_enabled("MIN"):
                    self._log_event(
                        origin="system",
                        tid=str(tid),
                        name=name,
                        source_num=source_num,
                        category="ack",
                        field=f"{field}.timeout",
                        old=ack.get("expected"),
                        new=new_value,
                        msg=f"ACK timeout (origin={ack.get('origin')})",
                    )
                return
            exp = ack.get("expected")
            ok = False
            try:
                if field == "setpoint":
                    ok = (_as_float(exp) is not None and _as_float(new_value) is not None and abs(float(_as_float(exp)) - float(_as_float(new_value))) <= 0.2)
                else:
                    ok = str(exp).upper() == str(new_value).upper()
            except Exception:
                ok = False
            if ok:
                self._pending_acks.pop(k, None)
                if self._log_enabled("MIN"):
                    self._log_event(
                        origin="esafe",
                        tid=str(tid),
                        name=name,
                        source_num=source_num,
                        category="ack",
                        field=field,
                        old=exp,
                        new=new_value,
                        msg=f"ACK from e-safe (origin={ack.get('origin')})",
                    )
        except Exception:
            pass

    def _load_events(self) -> None:
        try:
            if not os.path.exists(EVENTS_PATH):
                return
            # Load last ~400 events (tail). File is JSONL.
            with open(EVENTS_PATH, "rb") as f:
                try:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 256 * 1024), os.SEEK_SET)
                except Exception:
                    f.seek(0)
                data = f.read().decode("utf-8", errors="ignore")
            lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
            out: List[Dict[str, Any]] = []
            for ln in lines[-400:]:
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
            with self._events_lock:
                self._events = out[-400:]
            try:
                self.state.set_meta("e_therm_events", self._events[-200:])
                self._update_events_meta_stats(recount=True)
            except Exception:
                pass
        except Exception:
            pass

    def _update_events_meta_stats(self, *, recount: bool = False, delta: int = 0) -> None:
        try:
            file_bytes = int(os.path.getsize(EVENTS_PATH)) if os.path.exists(EVENTS_PATH) else 0
        except Exception:
            file_bytes = 0
        file_events = int(self._events_file_count or 0)
        if recount:
            try:
                c = 0
                if os.path.exists(EVENTS_PATH):
                    with open(EVENTS_PATH, "rb") as f:
                        for ln in f:
                            if ln.strip():
                                c += 1
                file_events = int(c)
            except Exception:
                file_events = int(self._events_file_count or 0)
        elif delta:
            file_events = max(0, int(file_events) + int(delta))
        self._events_file_count = int(file_events)
        try:
            self.state.set_meta(
                "e_therm_events_meta",
                {
                    "file_bytes": int(file_bytes),
                    "file_kb": round(float(file_bytes) / 1024.0, 1),
                    "file_events": int(file_events),
                },
            )
        except Exception:
            pass

    def _log_event(
        self,
        *,
        origin: str,
        tid: Optional[str],
        name: Optional[str],
        source_num: Optional[int],
        category: str,
        field: str,
        old: Any = None,
        new: Any = None,
        msg: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            ts = time.time()
            try:
                self._event_seq = (int(self._event_seq) + 1) % 1000
            except Exception:
                self._event_seq = 0
            eid = int(ts * 1000) * 1000 + int(self._event_seq)
            ev: Dict[str, Any] = {
                "ts": ts,
                "id": eid,
                "origin": str(origin or "unknown"),
                "tid": str(tid) if tid is not None else None,
                "name": str(name) if name is not None else None,
                "source_num": int(source_num) if source_num is not None else None,
                "category": str(category or ""),
                "field": str(field or ""),
                "old": old,
                "new": new,
                "msg": str(msg or ""),
            }
            if extra and isinstance(extra, dict):
                ev["extra"] = extra
            line = json.dumps(ev, ensure_ascii=False)
            try:
                os.makedirs(os.path.dirname(EVENTS_PATH) or "/", exist_ok=True)
                with open(EVENTS_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._trim_events_file_if_needed()
            except Exception:
                pass

            # Also publish as a real "logs" entity so /logs updates live via SSE.
            try:
                lt = time.localtime(ts)
                log_item = {
                    "ID": int(eid),
                    "TYPE": "E-THERM",
                    "DATA": time.strftime("%Y-%m-%d", lt),
                    "TIME": time.strftime("%H:%M:%S", lt),
                    "EV": (f"{category}:{field}".strip(":") or "event"),
                    "I1": f"{origin} | tid={tid or ''} {name or ''} | {msg}".strip(),
                    "I2": json.dumps({"old": old, "new": new}, ensure_ascii=False),
                    "IML": "",
                    "ORI": str(origin or ""),
                    "TID": str(tid or ""),
                }
                self.state.apply_realtime_update("logs", [log_item])
            except Exception:
                pass

            with self._events_lock:
                self._events.append(ev)
                # keep last 800 in memory
                if len(self._events) > 800:
                    self._events = self._events[-800:]
                snap = self._events[-200:]
            try:
                self.state.set_meta("e_therm_events", snap)
                self._update_events_meta_stats(delta=1)
            except Exception:
                pass
        except Exception:
            pass

    def _trim_events_file_if_needed(self) -> None:
        """Keep EVENTS_PATH bounded by size, retaining the newest events."""
        try:
            max_kb = int(self.log_file_max_kb or 0)
        except Exception:
            max_kb = 0
        if max_kb <= 0:
            return
        max_bytes = int(max_kb) * 1024
        try:
            size = os.path.getsize(EVENTS_PATH)
        except Exception:
            return
        if size <= max_bytes:
            return

        # Keep ~80% of the max to reduce frequent trims.
        keep_bytes = int(max(4096, max_bytes * 0.8))
        tmp = EVENTS_PATH + ".tmp"
        try:
            with open(EVENTS_PATH, "rb") as f:
                try:
                    f.seek(0, os.SEEK_END)
                    end = f.tell()
                    start = max(0, end - keep_bytes)
                    f.seek(start, os.SEEK_SET)
                except Exception:
                    f.seek(0)
                data = f.read()
        except Exception:
            return

        # Ensure we start at a line boundary (JSONL).
        try:
            if b"\n" in data:
                if data[:1] != b"{" and data[:1] != b"[":
                    # If we started mid-line, drop until first newline.
                    nl = data.find(b"\n")
                    if nl != -1 and nl + 1 < len(data):
                        data = data[nl + 1 :]
            # Ensure trailing newline.
            if data and not data.endswith(b"\n"):
                data += b"\n"
        except Exception:
            pass

        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, EVENTS_PATH)
            self._update_events_meta_stats(recount=True)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _create_mqtt_client(self) -> mqtt.Client:
        client_id = f"e-therm-plus-{int(time.time())}"
        # Keep client creation conservative to avoid runtime mismatch between
        # callback API versions across environments.
        try:
            c = mqtt.Client(
                client_id=client_id,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            )
        except Exception:
            c = mqtt.Client(client_id=client_id)
        user = (self.opts.get("mqtt_user") or "").strip()
        pw = (self.opts.get("mqtt_password") or "")
        if user:
            c.username_pw_set(user, pw)
        try:
            c.will_set(f"{self.out_prefix}/status", "offline", retain=True)
        except Exception:
            pass
        # Dispatchers are tolerant to paho callback signature differences.
        c.on_connect = self._on_connect_dispatch
        c.on_message = self._on_message
        c.on_disconnect = self._on_disconnect_dispatch
        try:
            # Conservative auto-reconnect delays handled by paho (best effort)
            c.reconnect_delay_set(min_delay=2, max_delay=30)
        except Exception:
            pass
        return c

    def _mqtt_target(self) -> tuple[str, int]:
        """Read MQTT host/port from live options.json first, then fallback to cached opts."""
        try:
            live = load_options()
            if isinstance(live, dict):
                host_live = str(live.get("mqtt_host") or "").strip()
                host_cached = str(self.opts.get("mqtt_host") or "").strip()
                host = host_live or host_cached or "core-mosquitto"
                port = int(live.get("mqtt_port", self.opts.get("mqtt_port", 1883)) or 1883)
                # Keep a synced in-memory view for watchdog/config reads.
                self.opts["mqtt_host"] = host
                self.opts["mqtt_port"] = port
                if host == "core-mosquitto":
                    print("[WARN] mqtt_host fallback to core-mosquitto (live/cached empty)")
                return host, port
        except Exception:
            pass
        return str(self.opts.get("mqtt_host", "core-mosquitto")).strip(), int(self.opts.get("mqtt_port", 1883) or 1883)

    def _on_connect_dispatch(self, *args, **kwargs):
        try:
            return self._on_connect(*args, **kwargs)
        except TypeError:
            # Fallback for environments that still invoke legacy signatures.
            try:
                return self._on_connect(*args[:4])
            except Exception:
                return None

    def _on_disconnect_dispatch(self, *args, **kwargs):
        try:
            return self._on_disconnect(*args, **kwargs)
        except TypeError:
            try:
                return self._on_disconnect(*args[:3])
            except Exception:
                return None

    def _auto_enabled_for(self, t: Dict[str, Any]) -> bool:
        if self._display_only_for(t):
            return False
        try:
            if isinstance(t, dict) and t.get("auto_control_enabled") is not None:
                return bool(t.get("auto_control_enabled"))
        except Exception:
            pass
        return bool(self.auto_control_enabled)

    def _display_only_for(self, t: Dict[str, Any]) -> bool:
        try:
            return bool(isinstance(t, dict) and (t.get("display_only") or t.get("view_only") or t.get("read_only")))
        except Exception:
            return False

    def _override_sec_for(self, t: Dict[str, Any]) -> int:
        try:
            v = t.get("auto_override_sec") if isinstance(t, dict) else None
            if v is not None:
                return int(v)
        except Exception:
            pass
        try:
            return int(self.auto_override_sec)
        except Exception:
            return 300

    # -------------------- Config --------------------

    def therm_list(self):
        return self.cfg.get("thermostats") or []

    def _find_by_id(self, tid: str) -> Optional[Dict[str, Any]]:
        for t in self.therm_list():
            if str(t.get("id")) == str(tid):
                return t
        return None

    def _find_by_source_num(self, num: int) -> Optional[Dict[str, Any]]:
        for t in self.therm_list():
            src = t.get("source") or {}
            try:
                if str(src.get("type", "")).lower() not in ("esafe", "esafe_json"):
                    continue
                src_num = int(src.get("num", -1))
            except Exception:
                continue
            if src_num == int(num):
                return t
        return None

    def _therm_climate_cfg(self, t: Dict[str, Any]) -> Dict[str, Any]:
        cfg = t.get("climate") if isinstance(t, dict) else None
        out = cfg if isinstance(cfg, dict) else {}
        return out

    def _therm_allowed_modes(self, t: Dict[str, Any]) -> List[str]:
        cfg = self._therm_climate_cfg(t)
        raw = cfg.get("modes")
        modes: List[str] = []
        if isinstance(raw, list):
            for m in raw:
                sm = str(m or "").strip().lower()
                if sm in ("off", "heat", "cool") and sm not in modes:
                    modes.append(sm)
        if not modes:
            modes = ["off", "heat", "cool"]
        return modes

    def _therm_temp_bounds(self, t: Dict[str, Any]) -> tuple[float, float]:
        cfg = self._therm_climate_cfg(t)
        tmin = _as_float(cfg.get("min_temp"))
        tmax = _as_float(cfg.get("max_temp"))
        if tmin is None:
            tmin = 5.0
        if tmax is None:
            tmax = 35.0
        if float(tmax) <= float(tmin):
            return 5.0, 35.0
        return float(tmin), float(tmax)

    def _clamp_therm_target(self, t: Dict[str, Any], value: float) -> float:
        tmin, tmax = self._therm_temp_bounds(t)
        return max(float(tmin), min(float(tmax), float(value)))

    def _ha_climate_terms(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for t in self.therm_list():
            src = t.get("source") or {}
            st = str(src.get("type", "")).lower()
            if st in ("ha_climate", "homeassistant_climate", "ha"):
                ent = str(src.get("entity_id") or "").strip()
                if ent:
                    out.append(t)
        return out

    def _ha_multi_sensor_terms(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for t in self.therm_list():
            src = t.get("source") or {}
            st = str(src.get("type", "")).lower()
            if st not in ("ha_multi_sensor_avg", "ha_sensor_avg", "ha_multi_avg"):
                continue
            sensors = src.get("sensors")
            if isinstance(sensors, list) and sensors:
                out.append(t)
        return out

    def _ha_sensor_terms(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for t in self.therm_list():
            src = t.get("source") or {}
            st = str(src.get("type", "")).lower()
            if st not in ("ha_sensor", "homeassistant_sensor", "sensor"):
                continue
            ent = str(src.get("entity_id") or "").strip()
            sensors = src.get("sensors") if isinstance(src.get("sensors"), list) else []
            if ent or sensors:
                out.append(t)
        return out

    def _virtual_terms(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for t in self.therm_list():
            src = t.get("source") or {}
            st = str(src.get("type", "")).lower()
            if st in ("virtual", "local", "local_virtual"):
                out.append(t)
        return out

    def _sync_virtual_states(self, force: bool = False) -> None:
        terms = self._virtual_terms()
        if not terms:
            return
        any_update = False
        now = time.time()

        for t in terms:
            tid = str(t.get("id"))
            src = t.get("source") if isinstance(t.get("source"), dict) else {}
            initial_temp = _as_float(
                _get_any(src, "current_temperature", "temperature", "initial_temperature")
            )
            if initial_temp is None:
                initial_temp = 20.0
            initial_target = _as_float(
                _get_any(src, "target_temperature", "target", "initial_target")
            )
            if initial_target is None:
                initial_target = 21.0
            rh = _as_float(_get_any(src, "humidity", "initial_humidity"))
            season = str(_get_any(src, "season", "act_sea") or "WIN").strip().upper()
            if season not in ("WIN", "SUM", "OFF"):
                season = "WIN"
            allowed_modes = self._therm_allowed_modes(t)
            if "heat" not in allowed_modes and "cool" in allowed_modes and season == "WIN":
                season = "SUM"
            preset = str(_get_any(src, "preset", "mode", "act_model") or "MAN").strip().upper()
            if not preset:
                preset = "MAN"

            with self.lock:
                rt = self.rt.setdefault(tid, {})
                th = rt.setdefault("THERM", {})
                if force or rt.get("TEMP") is None:
                    rt["TEMP"] = float(initial_temp)
                    any_update = True
                if rh is not None and (force or rt.get("RH") is None):
                    rt["RH"] = float(rh)
                    any_update = True
                thr = th.get("TEMP_THR") if isinstance(th.get("TEMP_THR"), dict) else None
                if force or not (thr and thr.get("VAL") is not None):
                    th["TEMP_THR"] = {"VAL": float(initial_target)}
                    any_update = True
                if force or not th.get("ACT_SEA"):
                    th["ACT_SEA"] = season
                    any_update = True
                if force or not th.get("ACT_MODEL"):
                    th["ACT_MODEL"] = "OFF" if season == "OFF" else preset
                    any_update = True
                if force or not th.get("OUT_STATUS"):
                    th["OUT_STATUS"] = "OFF"
                    any_update = True

                # Optional dev/test simulation. Disabled by default; when enabled it
                # nudges the virtual current temperature based on demand state.
                if bool(src.get("simulate_temperature", src.get("simulate", False))):
                    last_key = "_last_virtual_sim_ts"
                    last_map = self.runtime.get(last_key)
                    if not isinstance(last_map, dict):
                        last_map = {}
                    last_ts = _as_float(last_map.get(tid))
                    last_map[tid] = now
                    self.runtime[last_key] = last_map
                    if last_ts is not None and now > float(last_ts):
                        dt_min = min(10.0, max(0.0, (now - float(last_ts)) / 60.0))
                        cur = _as_float(rt.get("TEMP"))
                        if cur is not None and dt_min > 0:
                            demand = str(th.get("DEMAND_ON") or "").upper() == "ON"
                            sea = str(th.get("ACT_SEA") or "WIN").upper()
                            ambient = _as_float(src.get("ambient_temperature"))
                            if ambient is None:
                                ambient = float(initial_temp)
                            heat_rate = _as_float(src.get("heat_rate_per_min"))
                            cool_rate = _as_float(src.get("cool_rate_per_min"))
                            drift_rate = _as_float(src.get("drift_rate_per_min"))
                            heat_rate = float(heat_rate) if heat_rate is not None else 0.08
                            cool_rate = float(cool_rate) if cool_rate is not None else 0.08
                            drift_rate = float(drift_rate) if drift_rate is not None else 0.02
                            next_temp = float(cur)
                            if demand and sea == "WIN":
                                next_temp += heat_rate * dt_min
                            elif demand and sea == "SUM":
                                next_temp -= cool_rate * dt_min
                            else:
                                delta = float(ambient) - next_temp
                                step = max(-drift_rate * dt_min, min(drift_rate * dt_min, delta))
                                next_temp += step
                            rt["TEMP"] = round(float(next_temp), 2)
                            any_update = True

        if any_update:
            try:
                self._last_source_ts = time.time()
                self._ever_got_source = True
            except Exception:
                pass
            self._sync_ui()
            self._persist_rt_cache()

    def _ha_api_request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
        if not token:
            now = time.time()
            if (now - float(self._last_ha_warn_ts or 0.0)) > 60.0:
                self._last_ha_warn_ts = now
                print("[HA_API] SUPERVISOR_TOKEN missing, ha_climate sync unavailable")
            return None
        url = f"http://supervisor/core/api{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except Exception:
                    return None
        except urllib.error.HTTPError as e:
            now = time.time()
            if (now - float(self._last_ha_warn_ts or 0.0)) > 60.0:
                self._last_ha_warn_ts = now
                print(f"[HA_API] HTTP {int(getattr(e, 'code', 0) or 0)} on {method.upper()} {path}")
            return None
        except Exception as e:
            now = time.time()
            if (now - float(self._last_ha_warn_ts or 0.0)) > 60.0:
                self._last_ha_warn_ts = now
                print(f"[HA_API] request failed {method.upper()} {path}: {e}")
            return None

    def _poll_ha_climate_states(self, force: bool = False) -> None:
        terms = self._ha_climate_terms()
        if not terms:
            return
        now = time.time()
        poll_interval = self._opt_seconds("ha_poll_interval_sec", 15.0, 5.0)
        if not force and (now - float(self._last_ha_poll_ts or 0.0)) < poll_interval:
            return
        self._last_ha_poll_ts = now

        for t in terms:
            tid = str(t.get("id"))
            src = t.get("source") or {}
            ent = str(src.get("entity_id") or "").strip()
            if not ent:
                continue
            st = self._ha_api_request("GET", f"/states/{ent}")
            if not isinstance(st, dict):
                continue
            attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
            if not isinstance(attrs, dict):
                attrs = {}
            cur = _as_float(attrs.get("current_temperature"))
            rh = _as_float(attrs.get("current_humidity"))
            tgt = _as_float(attrs.get("temperature"))
            hvac = str(st.get("state") or "").strip().lower()
            hvac_action = str(attrs.get("hvac_action") or "").strip().lower()
            preset = str(attrs.get("preset_mode") or "").strip().upper()
            hold = self._ha_bridge_mode_hold.get(tid)
            if isinstance(hold, dict):
                hold_until = float(hold.get("until", 0.0) or 0.0)
                hold_mode = str(hold.get("mode") or "").strip().lower()
                if now <= hold_until and hvac == "off" and hold_mode in ("heat", "cool"):
                    hvac = hold_mode
                    preset = preset or "MAN"
                elif now > hold_until:
                    self._ha_bridge_mode_hold.pop(tid, None)

            with self.lock:
                rt = self.rt.setdefault(tid, {})
                th = rt.setdefault("THERM", {})
                if cur is not None:
                    rt["TEMP"] = float(cur)
                if rh is not None:
                    rt["RH"] = float(rh)
                if tgt is not None:
                    th["TEMP_THR"] = {"VAL": float(tgt)}
                if hvac == "cool":
                    th["ACT_SEA"] = "SUM"
                elif hvac == "heat":
                    th["ACT_SEA"] = "WIN"
                elif hvac == "off":
                    th["ACT_MODEL"] = "OFF"
                if preset:
                    th["ACT_MODEL"] = preset
                elif hvac in ("heat", "cool"):
                    th["ACT_MODEL"] = "MAN"
                if hvac_action in ("heating", "cooling"):
                    out_status = "ON"
                elif hvac_action in ("idle", "off"):
                    out_status = "OFF"
                else:
                    out_status = "OFF" if hvac == "off" else "ON"
                th["OUT_STATUS"] = out_status

            try:
                self._last_source_ts = time.time()
                self._ever_got_source = True
            except Exception:
                pass

        self._sync_ui()
        self._persist_rt_cache()

    def _real_thermostat_cfg(self, t: Dict[str, Any]) -> Dict[str, Any]:
        cfg = t.get("real_thermostat")
        return cfg if isinstance(cfg, dict) else {}

    def _real_thermostat_entity(self, t: Dict[str, Any]) -> str:
        cfg = self._real_thermostat_cfg(t)
        ent = str(cfg.get("entity_id") or "").strip()
        if ent:
            return ent
        src = t.get("source") or {}
        ent = str(src.get("real_entity_id") or "").strip()
        if ent:
            return ent
        src_type = str(src.get("type") or "").strip().lower()
        src_ent = str(src.get("entity_id") or "").strip()
        if src_type in ("ha_sensor", "homeassistant_sensor", "sensor") and src_ent.startswith("climate."):
            return src_ent
        return ""

    def _ha_helper_climate_entity(self, t: Dict[str, Any]) -> str:
        src = t.get("source") if isinstance(t.get("source"), dict) else {}
        ent = str(
            src.get("helper_climate_entity_id")
            or src.get("climate_entity_id")
            or src.get("setpoint_climate_entity_id")
            or ""
        ).strip()
        return ent if ent.startswith("climate.") else ""

    def _bool_cfg(self, d: Dict[str, Any], key: str, default: bool) -> bool:
        try:
            v = d.get(key)
            if v is None:
                return bool(default)
            return bool(v)
        except Exception:
            return bool(default)

    def _ha_state_temperature(self, st: Dict[str, Any]) -> Optional[float]:
        if not isinstance(st, dict):
            return None
        attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
        ent_id = str(st.get("entity_id") or "").strip()
        if ent_id.startswith("climate."):
            v = _as_float(attrs.get("current_temperature"))
            if v is None:
                v = _as_float(attrs.get("DISPLAY_TEMPERATURE"))
            if v is None:
                v = _as_float(attrs.get("TEMPERATURE"))
            return v
        v = _as_float(st.get("state"))
        if v is None:
            v = _as_float(attrs.get("temperature"))
        if v is None:
            v = _as_float(attrs.get("current_temperature"))
        return v

    def _ha_state_humidity(self, st: Dict[str, Any]) -> Optional[float]:
        if not isinstance(st, dict):
            return None
        attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
        v = _as_float(st.get("state"))
        if v is None:
            v = _as_float(attrs.get("current_humidity"))
        if v is None:
            v = _as_float(attrs.get("humidity"))
        return v

    def _ha_state_is_fresh(self, st: Dict[str, Any], stale_sec: float) -> bool:
        if stale_sec <= 0:
            return True
        dt = _parse_iso_datetime(st.get("last_updated") if isinstance(st, dict) else None)
        if dt is None:
            dt = _parse_iso_datetime(st.get("last_changed") if isinstance(st, dict) else None)
        if dt is None:
            return True
        age = time.time() - dt.timestamp()
        return age <= float(stale_sec)

    def _poll_ha_multi_sensor_states(self, force: bool = False) -> None:
        terms = self._ha_multi_sensor_terms()
        if not terms:
            return
        now = time.time()
        poll_interval = self._opt_seconds("ha_poll_interval_sec", 15.0, 5.0)
        if not force and (now - float(self._last_ha_plain_sensor_poll_ts or 0.0)) < poll_interval:
            return
        self._last_ha_plain_sensor_poll_ts = now

        any_update = False
        for t in terms:
            tid = str(t.get("id"))
            src = t.get("source") if isinstance(t.get("source"), dict) else {}
            sensors = src.get("sensors") if isinstance(src.get("sensors"), list) else []
            sensors = [str(x).strip() for x in sensors if str(x).strip()]
            if not sensors:
                continue
            min_valid = _as_int(src.get("min_valid_sensors"))
            if min_valid is None:
                min_valid = len(sensors)
            min_valid = max(1, min(int(min_valid), len(sensors)))
            stale_sec = _as_float(src.get("stale_sec"))
            stale_sec = float(stale_sec) if stale_sec is not None else 0.0

            vals: List[float] = []
            sensor_rows: List[Dict[str, Any]] = []
            for ent in sensors:
                st = self._ha_api_request("GET", f"/states/{ent}")
                if not isinstance(st, dict):
                    sensor_rows.append({"entity_id": ent, "name": ent, "temp": None, "status": "unavailable"})
                    continue
                attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
                name = str(attrs.get("friendly_name") or ent).strip() or ent
                if not self._ha_state_is_fresh(st, stale_sec):
                    sensor_rows.append({"entity_id": ent, "name": name, "temp": None, "status": "stale"})
                    continue
                v = self._ha_state_temperature(st)
                if v is None:
                    sensor_rows.append({"entity_id": ent, "name": name, "temp": None, "status": "invalid"})
                    continue
                vals.append(float(v))
                sensor_rows.append({"entity_id": ent, "name": name, "temp": float(v), "status": "ok"})

            if len(vals) < int(min_valid):
                continue
            avg = float(sum(vals) / len(vals))
            rh = None
            humidity_ent = str(src.get("humidity_entity_id") or src.get("rh_entity_id") or "").strip()
            if humidity_ent:
                st_h = self._ha_api_request("GET", f"/states/{humidity_ent}")
                if isinstance(st_h, dict) and self._ha_state_is_fresh(st_h, stale_sec):
                    rh = self._ha_state_humidity(st_h)

            th_patch: Dict[str, Any] = {}
            helper_ent = self._ha_helper_climate_entity(t)
            if helper_ent:
                st = self._ha_api_request("GET", f"/states/{helper_ent}")
                if isinstance(st, dict):
                    attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
                    helper_tgt = _as_float(attrs.get("temperature"))
                    helper_hvac = str(st.get("state") or "").strip().lower()
                    helper_preset = str(attrs.get("preset_mode") or "").strip().upper()
                    helper_action = str(attrs.get("hvac_action") or "").strip().lower()
                    if helper_tgt is not None:
                        th_patch["TEMP_THR"] = {"VAL": float(helper_tgt)}
                    if helper_hvac == "heat":
                        th_patch["ACT_SEA"] = "WIN"
                        th_patch["ACT_MODEL"] = helper_preset or "MAN"
                    elif helper_hvac == "cool":
                        th_patch["ACT_SEA"] = "SUM"
                        th_patch["ACT_MODEL"] = helper_preset or "MAN"
                    elif helper_hvac == "off":
                        th_patch["ACT_SEA"] = "OFF"
                        th_patch["ACT_MODEL"] = "OFF"
                    if helper_action in ("heating", "cooling"):
                        th_patch["HELPER_HVAC_ACTION"] = str(helper_action).upper()
                    th_patch["HELPER_CLIMATE"] = helper_ent
            real_ent = self._real_thermostat_entity(t)
            if real_ent:
                st = self._ha_api_request("GET", f"/states/{real_ent}")
                if isinstance(st, dict):
                    attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
                    real_cur = _as_float(attrs.get("current_temperature"))
                    tgt = _as_float(attrs.get("temperature"))
                    hvac = str(st.get("state") or "").strip().lower()
                    hvac_action = str(attrs.get("hvac_action") or "").strip().lower()
                    if real_cur is not None:
                        th_patch["REAL_TEMP"] = float(real_cur)
                    # Keep real thermostat telemetry separate from virtual control state.
                    # Do not overwrite virtual TEMP_THR / ACT_SEA / ACT_MODEL from real climate.
                    if tgt is not None:
                        th_patch["REAL_TARGET"] = float(tgt)
                    if hvac:
                        th_patch["REAL_HVAC"] = str(hvac).upper()
                        th_patch["REAL_HVAC_STATE"] = str(hvac).upper()
                    if hvac_action:
                        th_patch["REAL_HVAC_ACTION"] = str(hvac_action).upper()
                        if hvac_action in ("heating", "cooling"):
                            th_patch["OUT_STATUS"] = "ON"
                        elif hvac_action in ("idle", "off"):
                            th_patch["OUT_STATUS"] = "OFF"

            with self.lock:
                rt = self.rt.setdefault(tid, {})
                rt["TEMP"] = float(avg)
                rt["AVG_TEMP"] = float(avg)
                if rh is not None:
                    rt["RH"] = float(rh)
                rt["AVG_VALID"] = int(len(vals))
                rt["AVG_COUNT"] = int(len(sensors))
                rt["AVG_SENSORS"] = sensor_rows
                th = rt.setdefault("THERM", {})
                if "DEMAND_ON" not in th:
                    th["DEMAND_ON"] = "OFF"
                if "DEMAND_REASON" not in th:
                    th["DEMAND_REASON"] = "WAIT_CONTROL_LOOP"
                if not th.get("ACT_SEA"):
                    th["ACT_SEA"] = "WIN"
                if not th.get("ACT_MODEL"):
                    th["ACT_MODEL"] = "MAN"
                for k, v in th_patch.items():
                    th[k] = v
                if th_patch.get("REAL_TEMP") is not None:
                    rt["REAL_TEMP"] = float(th_patch.get("REAL_TEMP"))
            any_update = True

        if any_update:
            try:
                self._last_source_ts = time.time()
                self._ever_got_source = True
            except Exception:
                pass
            self._sync_ui()
            self._persist_rt_cache()

    def _poll_ha_sensor_states(self, force: bool = False) -> None:
        terms = self._ha_sensor_terms()
        if not terms:
            return
        now = time.time()
        poll_interval = self._opt_seconds("ha_poll_interval_sec", 15.0, 5.0)
        if not force and (now - float(self._last_ha_sensor_poll_ts or 0.0)) < poll_interval:
            return
        self._last_ha_sensor_poll_ts = now

        any_update = False
        for t in terms:
            tid = str(t.get("id"))
            src = t.get("source") if isinstance(t.get("source"), dict) else {}
            sensors = src.get("sensors") if isinstance(src.get("sensors"), list) else []
            sensors = [str(x).strip() for x in sensors if str(x).strip()]
            ent = str(src.get("entity_id") or "").strip()
            if ent and ent not in sensors:
                sensors.insert(0, ent)
            if not sensors:
                continue

            min_valid = _as_int(src.get("min_valid_sensors"))
            if min_valid is None:
                min_valid = 1
            min_valid = max(1, min(int(min_valid), len(sensors)))
            stale_sec = _as_float(src.get("stale_sec"))
            stale_sec = float(stale_sec) if stale_sec is not None else 0.0

            vals: List[float] = []
            sensor_rows: List[Dict[str, Any]] = []
            for ent0 in sensors:
                st = self._ha_api_request("GET", f"/states/{ent0}")
                if not isinstance(st, dict):
                    sensor_rows.append({"entity_id": ent0, "name": ent0, "temp": None, "status": "unavailable"})
                    continue
                attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
                name = str(attrs.get("friendly_name") or ent0).strip() or ent0
                if not self._ha_state_is_fresh(st, stale_sec):
                    sensor_rows.append({"entity_id": ent0, "name": name, "temp": None, "status": "stale"})
                    continue
                v = self._ha_state_temperature(st)
                if v is None:
                    sensor_rows.append({"entity_id": ent0, "name": name, "temp": None, "status": "invalid"})
                    continue
                vals.append(float(v))
                sensor_rows.append({"entity_id": ent0, "name": name, "temp": float(v), "status": "ok"})

            if len(vals) < int(min_valid):
                continue
            cur = float(sum(vals) / len(vals))
            rh = None
            humidity_ent = str(src.get("humidity_entity_id") or src.get("rh_entity_id") or "").strip()
            if humidity_ent:
                st_h = self._ha_api_request("GET", f"/states/{humidity_ent}")
                if isinstance(st_h, dict) and self._ha_state_is_fresh(st_h, stale_sec):
                    rh = self._ha_state_humidity(st_h)
            initial_target = _as_float(_get_any(src, "target_temperature", "target", "initial_target"))
            if initial_target is None:
                initial_target = 21.0
            season = str(_get_any(src, "season", "act_sea") or "WIN").strip().upper()
            if season not in ("WIN", "SUM", "OFF"):
                season = "WIN"
            allowed_modes = self._therm_allowed_modes(t)
            if "heat" not in allowed_modes and "cool" in allowed_modes and season == "WIN":
                season = "SUM"
            real_target = None
            real_hvac = ""
            real_hvac_action = ""
            real_ent = self._real_thermostat_entity(t)
            if real_ent:
                st_real = self._ha_api_request("GET", f"/states/{real_ent}")
                if isinstance(st_real, dict):
                    attrs_real = st_real.get("attributes") if isinstance(st_real.get("attributes"), dict) else {}
                    real_hvac = str(st_real.get("state") or "").strip().lower()
                    real_hvac_action = str(attrs_real.get("hvac_action") or "").strip().lower()
                    real_target = _as_float(attrs_real.get("temperature"))
                    if real_target is None:
                        if real_hvac == "cool":
                            real_target = _as_float(attrs_real.get("target_temp_low") or attrs_real.get("DISPLAY_COOLSETPOINT"))
                        else:
                            real_target = _as_float(attrs_real.get("target_temp_high") or attrs_real.get("DISPLAY_HEATSETPOINT"))

            with self.lock:
                rt = self.rt.setdefault(tid, {})
                rt["TEMP"] = float(cur)
                rt["AVG_TEMP"] = float(cur)
                if rh is not None:
                    rt["RH"] = float(rh)
                rt["AVG_VALID"] = int(len(vals))
                rt["AVG_COUNT"] = int(len(sensors))
                rt["AVG_SENSORS"] = sensor_rows
                th = rt.setdefault("THERM", {})
                if not th.get("ACT_SEA"):
                    th["ACT_SEA"] = season
                if not th.get("ACT_MODEL"):
                    th["ACT_MODEL"] = "OFF" if season == "OFF" else "MAN"
                if not th.get("OUT_STATUS"):
                    th["OUT_STATUS"] = "OFF"
                thr = th.get("TEMP_THR") if isinstance(th.get("TEMP_THR"), dict) else None
                mode_hold = self._ha_bridge_mode_hold.get(str(tid))
                mode_hold_until = float(mode_hold.get("until", 0.0) if isinstance(mode_hold, dict) else 0.0)
                if real_hvac:
                    th["REAL_HVAC"] = str(real_hvac).upper()
                    th["REAL_HVAC_STATE"] = str(real_hvac).upper()
                if real_hvac_action:
                    th["REAL_HVAC_ACTION"] = str(real_hvac_action).upper()
                    if real_hvac_action in ("heating", "cooling"):
                        th["OUT_STATUS"] = "ON"
                    elif real_hvac_action in ("idle", "off"):
                        th["OUT_STATUS"] = "OFF"
                if real_hvac in ("heat", "cool", "off") and time.time() > mode_hold_until:
                    if real_hvac == "heat":
                        th["ACT_SEA"] = "WIN"
                        th["ACT_MODEL"] = "MAN"
                    elif real_hvac == "cool":
                        th["ACT_SEA"] = "SUM"
                        th["ACT_MODEL"] = "MAN"
                    else:
                        th["ACT_SEA"] = "OFF"
                        th["ACT_MODEL"] = "OFF"
                hold_until = float(self._ha_bridge_setpoint_hold.get(str(tid), 0.0) or 0.0)
                if real_target is not None and time.time() > hold_until:
                    old_target = _as_float(thr.get("VAL")) if isinstance(thr, dict) else None
                    if old_target is None or abs(float(old_target) - float(real_target)) >= 0.05:
                        th["TEMP_THR"] = {"VAL": float(real_target)}
                elif not (thr and thr.get("VAL") is not None):
                    th["TEMP_THR"] = {"VAL": float(initial_target)}
            any_update = True

        if any_update:
            try:
                self._last_source_ts = time.time()
                self._ever_got_source = True
            except Exception:
                pass
            self._sync_ui()
            self._persist_rt_cache()

    def _ha_climate_service(self, entity_id: str, service: str, data: Dict[str, Any]) -> bool:
        payload = {"entity_id": entity_id}
        payload.update(data or {})
        res = self._ha_api_request("POST", f"/services/climate/{service}", payload)
        return res is not None

    def _ha_bridge_recent(self, key: str, window_sec: float = 2.0) -> bool:
        now = time.time()
        k = str(key)
        last = float(self._ha_bridge_cmd_last.get(k, 0.0) or 0.0)
        if last and (now - last) < float(window_sec):
            return True
        self._ha_bridge_cmd_last[k] = now
        return False

    def _ha_climate_set_hvac_mode_safe(self, entity_id: str, mode: str) -> bool:
        ent = str(entity_id or "").strip()
        m = str(mode or "").strip().lower()
        if not ent or m not in ("heat", "cool", "off"):
            return False
        cur = self._ha_climate_state(ent)
        if cur == m:
            return True
        mode_window = 20.0 if m in ("heat", "cool") else 5.0
        if self._ha_bridge_recent(f"{ent}:mode:{m}", mode_window):
            return True
        ok = self._ha_climate_service(ent, "set_hvac_mode", {"hvac_mode": m})
        if ok or m == "off":
            return bool(ok)
        # Some climate integrations reject set_hvac_mode while off until turn_on.
        try:
            self._ha_service_call("climate", "turn_on", {"entity_id": ent})
        except Exception:
            pass
        return bool(self._ha_climate_service(ent, "set_hvac_mode", {"hvac_mode": m}))

    def _ha_climate_set_temperature_safe(self, entity_id: str, temperature: float, preferred_mode: str = "") -> bool:
        ent = str(entity_id or "").strip()
        if not ent:
            return False
        temp = float(temperature)
        mode = str(preferred_mode or "").strip().lower()
        cur_state = self._ha_climate_state(ent)
        cur_target = self._ha_climate_target(ent)
        if cur_target is not None and abs(float(cur_target) - float(temp)) < 0.05:
            if mode not in ("heat", "cool") or cur_state == mode:
                return True
        if self._ha_bridge_recent(f"{ent}:temp:{round(temp, 2)}:{mode}", 2.0):
            return True
        if mode in ("heat", "cool"):
            if cur_state != mode:
                self._ha_climate_set_hvac_mode_safe(ent, mode)
            if self._ha_climate_service(ent, "set_temperature", {"temperature": temp}):
                return True
            # Last fallback for platforms that only accept target updates when
            # the desired HVAC mode is included in the same service call.
            if self._ha_climate_service(ent, "set_temperature", {"temperature": temp, "hvac_mode": mode}):
                return True
        return bool(self._ha_climate_service(ent, "set_temperature", {"temperature": temp}))

    def _ha_service_call(self, domain: str, service: str, data: Dict[str, Any]) -> bool:
        res = self._ha_api_request("POST", f"/services/{domain}/{service}", data or {})
        return res is not None

    def _apply_real_vmc_demand(self, t: Dict[str, Any], demand_on: bool, sea: str) -> None:
        cfg = self._real_thermostat_cfg(t)
        vmc_raw = cfg.get("vmc_entity_id") or cfg.get("vmc_entities") or ""
        vmc_entities = [
            e for e in self._split_entities(vmc_raw)
            if e.startswith("fan.") or e.startswith("light.") or e.startswith("switch.")
        ]
        if not vmc_entities:
            return

        speed = _as_float(cfg.get("vmc_speed_pct"))
        if str(sea or "").upper() == "SUM":
            sp_c = _as_float(cfg.get("vmc_speed_pct_cool"))
            if sp_c is not None:
                speed = sp_c
        else:
            sp_h = _as_float(cfg.get("vmc_speed_pct_heat"))
            if sp_h is not None:
                speed = sp_h
        off_on_idle = self._bool_cfg(cfg, "vmc_off_on_no_demand", True)

        for ent in vmc_entities:
            cache_key = f"vmc:{ent}"
            domain = str(ent.split(".", 1)[0] if "." in ent else "").strip().lower()
            if demand_on:
                pct = int(max(1, min(100, round(float(speed if speed is not None else 100.0)))))
                desired = f"ON:{pct}" if domain in ("fan", "light") else "ON"
                if str(self._real_target_last.get(cache_key) or "") == desired:
                    continue
                ok = False
                if domain == "fan":
                    ok = self._ha_service_call("fan", "turn_on", {"entity_id": ent, "percentage": pct})
                    if not ok:
                        # Fallback for integrations that separate percentage and power calls.
                        ok = self._ha_service_call("fan", "set_percentage", {"entity_id": ent, "percentage": pct})
                        if ok:
                            self._ha_service_call("fan", "turn_on", {"entity_id": ent})
                elif domain == "light":
                    ok = self._ha_service_call("light", "turn_on", {"entity_id": ent, "brightness_pct": pct})
                    if not ok:
                        ok = self._ha_service_call("light", "turn_on", {"entity_id": ent})
                elif domain == "switch":
                    ok = self._ha_service_call("switch", "turn_on", {"entity_id": ent})
                if ok:
                    self._real_target_last[cache_key] = desired
            else:
                if not off_on_idle:
                    continue
                if str(self._real_target_last.get(cache_key) or "") == "OFF":
                    continue
                ok = False
                if domain == "fan":
                    ok = self._ha_service_call("fan", "turn_off", {"entity_id": ent})
                elif domain == "light":
                    ok = self._ha_service_call("light", "turn_off", {"entity_id": ent})
                elif domain == "switch":
                    ok = self._ha_service_call("switch", "turn_off", {"entity_id": ent})
                if ok:
                    self._real_target_last[cache_key] = "OFF"

    def _ha_climate_target(self, entity_id: str) -> Optional[float]:
        st = self._ha_api_request("GET", f"/states/{entity_id}")
        if not isinstance(st, dict):
            return None
        attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
        return _as_float(attrs.get("temperature"))

    def _ha_climate_state(self, entity_id: str) -> str:
        st = self._ha_api_request("GET", f"/states/{entity_id}")
        if not isinstance(st, dict):
            return ""
        return str(st.get("state") or "").strip().lower()

    def _apply_real_thermostat_demand(self, t: Dict[str, Any], demand_on: bool, sea: str) -> None:
        cfg = self._real_thermostat_cfg(t)
        ent = self._real_thermostat_entity(t)
        if not ent:
            return
        min_cycle = _as_int(cfg.get("min_cycle_sec"))
        if min_cycle is None:
            ctrl = t.get("control") if isinstance(t.get("control"), dict) else {}
            min_cycle = _as_int(ctrl.get("min_cycle_sec"))
        if min_cycle is None:
            min_cycle = 0
        min_cycle = max(0, int(min_cycle))

        key_state = f"cl:{ent}:demand"
        key_ts = f"cl:{ent}:demand_ts"
        old = str(self._real_target_last.get(key_state) or "")
        new = "ON" if bool(demand_on) else "OFF"
        now = time.time()
        src = t.get("source") if isinstance(t.get("source"), dict) else {}
        stype = str(src.get("type") or "").strip().lower()
        adaptive_cfg = cfg.get("adaptive_demand_setpoint")
        is_ha_avg = stype in ("ha_multi_sensor_avg", "ha_sensor_avg", "ha_multi_avg")
        # For multi-sensor virtual thermostats, adaptive bridge is mandatory.
        adaptive = True if is_ha_avg else (bool(adaptive_cfg) if adaptive_cfg is not None else False)

        mode = "cool" if str(sea).upper() == "SUM" else "heat"
        try:
            self._apply_real_vmc_demand(t, bool(demand_on), sea)
        except Exception:
            pass
        if not demand_on:
            last_ts = float(self._real_target_last.get(key_ts, 0.0) or 0.0)
            try:
                tid = str(t.get("id"))
                with self.lock:
                    rt = self.rt.setdefault(tid, {})
                    th = rt.setdefault("THERM", {})
                    th["DEMAND_ON"] = "OFF"
            except Exception:
                pass
            # For multi-sensor average virtual thermostats, OFF must be immediate.
            if is_ha_avg:
                min_cycle = 0
            # Apply min_cycle only when switching OFF, so ON demand is never delayed.
            if old and old != "OFF" and min_cycle > 0 and last_ts and (now - last_ts) < float(min_cycle):
                try:
                    tid = str(t.get("id"))
                    with self.lock:
                        rt = self.rt.setdefault(tid, {})
                        th = rt.setdefault("THERM", {})
                        th["DEMAND_REASON"] = "MIN_CYCLE_HOLD_OFF"
                except Exception:
                    pass
                return
            hv_before = self._ha_climate_state(ent)
            if old == "OFF" and hv_before == "off":
                st = self._real_therm_adapt.get(ent)
                if isinstance(st, dict):
                    st["delta_heat"] = float(cfg.get("demand_delta_base_heat", 1.0) or 1.0)
                    st["delta_cool"] = float(cfg.get("demand_delta_base_cool", 1.0) or 1.0)
                try:
                    tid = str(t.get("id"))
                    with self.lock:
                        rt = self.rt.setdefault(tid, {})
                        th = rt.setdefault("THERM", {})
                        th["DEMAND_REASON"] = "OFF_OK_ALREADY"
                        th["REAL_HVAC_STATE"] = "off"
                except Exception:
                    pass
                return
            ok_turn_off = self._ha_service_call("climate", "turn_off", {"entity_id": ent})
            # Some integrations only honor explicit hvac_mode OFF.
            ok_hvac_off = self._ha_climate_service(ent, "set_hvac_mode", {"hvac_mode": "off"})
            hv_now2 = self._ha_climate_state(ent)
            ok = (hv_now2 == "off")
            if ok:
                self._real_target_last[key_state] = "OFF"
                self._real_target_last[key_ts] = now
                st = self._real_therm_adapt.get(ent)
                if isinstance(st, dict):
                    st["delta_heat"] = float(cfg.get("demand_delta_base_heat", 1.0) or 1.0)
                    st["delta_cool"] = float(cfg.get("demand_delta_base_cool", 1.0) or 1.0)
                    st["last_step_heat"] = 0.0
                    st["last_step_cool"] = 0.0
                try:
                    tid = str(t.get("id"))
                    with self.lock:
                        rt = self.rt.setdefault(tid, {})
                        th = rt.setdefault("THERM", {})
                        th["DEMAND_REASON"] = "OFF_OK"
                        th["REAL_HVAC_STATE"] = "off"
                except Exception:
                    pass
            else:
                # Keep demand state as ON in cache until HA reports actual OFF, so next cycle retries.
                self._real_target_last[key_state] = "ON"
                try:
                    tid = str(t.get("id"))
                    with self.lock:
                        rt = self.rt.setdefault(tid, {})
                        th = rt.setdefault("THERM", {})
                        th["DEMAND_REASON"] = "OFF_RETRY"
                        th["REAL_HVAC_STATE"] = str(hv_now2 or "")
                        th["OFF_CMD_RESULT"] = f"turn_off={1 if ok_turn_off else 0},hvac_off={1 if ok_hvac_off else 0}"
                except Exception:
                    pass
            return

        if not adaptive:
            if old == "ON":
                return
            ok = self._ha_service_call("climate", "turn_on", {"entity_id": ent})
            if not ok:
                ok = self._ha_climate_service(ent, "set_hvac_mode", {"hvac_mode": mode})
            if ok:
                self._real_target_last[key_state] = "ON"
                self._real_target_last[key_ts] = now
            return

        # Adaptive setpoint mode: keep the real thermostat "ahead" of its local ambient sensor.
        try:
            base_h = float(cfg.get("demand_delta_base_heat", 1.0) or 1.0)
        except Exception:
            base_h = 1.0
        try:
            base_c = float(cfg.get("demand_delta_base_cool", 1.0) or 1.0)
        except Exception:
            base_c = 1.0
        try:
            step = float(cfg.get("demand_delta_step", 0.3) or 0.3)
        except Exception:
            step = 0.3
        try:
            step_sec = max(1, int(cfg.get("demand_delta_step_sec", 120) or 120))
        except Exception:
            step_sec = 120
        try:
            max_h = float(cfg.get("demand_delta_max_heat", cfg.get("demand_delta_max", 4.0)) or 4.0)
        except Exception:
            max_h = 4.0
        try:
            max_c = float(cfg.get("demand_delta_max_cool", cfg.get("demand_delta_max", 4.0)) or 4.0)
        except Exception:
            max_c = 4.0
        try:
            keepalive = max(10, int(cfg.get("demand_keepalive_sec", 90) or 90))
        except Exception:
            keepalive = 90

        st = self._real_therm_adapt.setdefault(ent, {})
        if old != "ON":
            st["delta_heat"] = base_h
            st["delta_cool"] = base_c
            st["last_step_heat"] = now
            st["last_step_cool"] = now
        cur_delta_key = "delta_cool" if mode == "cool" else "delta_heat"
        cur_step_key = "last_step_cool" if mode == "cool" else "last_step_heat"
        cur_base = base_c if mode == "cool" else base_h
        cur_max = max_c if mode == "cool" else max_h
        cur_delta = float(st.get(cur_delta_key, cur_base) or cur_base)
        last_step_ts = float(st.get(cur_step_key, 0.0) or 0.0)
        if old == "ON" and (now - last_step_ts) >= float(step_sec):
            cur_delta = min(float(cur_max), float(cur_delta + step))
            st[cur_delta_key] = cur_delta
            st[cur_step_key] = now
        else:
            st[cur_delta_key] = cur_delta

        # Try cached real ambient first, fallback to direct API state read.
        real_temp = None
        try:
            tid = str(t.get("id"))
            with self.lock:
                rt = self.rt.get(tid) or {}
                rv = rt.get("REAL_TEMP")
                if rv is None:
                    th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
                    rv = th.get("REAL_TEMP")
                real_temp = _as_float(rv)
        except Exception:
            real_temp = None
        if real_temp is None:
            st_obj = self._ha_api_request("GET", f"/states/{ent}")
            if isinstance(st_obj, dict):
                attrs = st_obj.get("attributes") if isinstance(st_obj.get("attributes"), dict) else {}
                real_temp = _as_float(attrs.get("current_temperature"))
                if real_temp is None:
                    real_temp = _as_float(st_obj.get("state"))

        # Ensure HVAC mode while demand is ON.
        mode_ok = True
        last_mode = str(st.get("last_mode") or "")
        if old != "ON" or last_mode != mode:
            mode_ok = self._ha_climate_service(ent, "set_hvac_mode", {"hvac_mode": mode})
            if not mode_ok:
                mode_ok = self._ha_service_call("climate", "turn_on", {"entity_id": ent})
            if mode_ok:
                st["last_mode"] = mode

        temp_ok = False
        if real_temp is not None:
            # Guarantee a minimum +/-1.0C gap from real ambient while demand is ON.
            # This prevents "stuck equal setpoint" behavior on integrations that
            # quickly satisfy when target == current ambient.
            try:
                min_gap_h = float(cfg.get("demand_min_gap_heat", 1.0) or 1.0)
            except Exception:
                min_gap_h = 1.0
            try:
                min_gap_c = float(cfg.get("demand_min_gap_cool", 1.0) or 1.0)
            except Exception:
                min_gap_c = 1.0
            eff_delta = max(float(cur_delta), float(min_gap_c if mode == "cool" else min_gap_h))
            target = float(real_temp - eff_delta) if mode == "cool" else float(real_temp + eff_delta)
            tmin = _as_float(cfg.get("demand_target_min", 5.0))
            tmax = _as_float(cfg.get("demand_target_max", 35.0))
            if mode == "cool":
                tmin = _as_float(cfg.get("demand_target_min_cool", tmin if tmin is not None else 5.0))
                tmax = _as_float(cfg.get("demand_target_max_cool", tmax if tmax is not None else 35.0))
            else:
                tmin = _as_float(cfg.get("demand_target_min_heat", tmin if tmin is not None else 5.0))
                tmax = _as_float(cfg.get("demand_target_max_heat", tmax if tmax is not None else 35.0))
            if tmin is None:
                tmin = 5.0
            if tmax is None:
                tmax = 35.0
            target = max(float(tmin), min(float(tmax), target))

            last_target = _as_float(st.get("last_target"))
            last_temp_ts = float(st.get("last_temp_ts", 0.0) or 0.0)
            need_send = (
                old != "ON"
                or last_target is None
                or abs(float(target) - float(last_target)) >= 0.1
                or (now - last_temp_ts) >= float(keepalive)
            )
            if need_send:
                target_send = round(float(target), 1)
                try:
                    tid = str(t.get("id"))
                    with self.lock:
                        rt = self.rt.setdefault(tid, {})
                        th = rt.setdefault("THERM", {})
                        th["DEMAND_ON"] = "ON"
                        th["ADAPT_TARGET"] = float(target_send)
                except Exception:
                    pass
                temp_ok = self._ha_climate_service(ent, "set_temperature", {"temperature": float(target_send)})
                if temp_ok:
                    rb = self._ha_climate_target(ent)
                    try:
                        tid = str(t.get("id"))
                        with self.lock:
                            rt = self.rt.setdefault(tid, {})
                            th = rt.setdefault("THERM", {})
                            if rb is not None:
                                th["REAL_TARGET_READ"] = float(rb)
                    except Exception:
                        pass
                    if rb is not None and abs(float(rb) - float(target_send)) > 0.3:
                        temp_ok = False
                if not temp_ok:
                    # Some integrations accept target changes only after explicit mode refresh.
                    try:
                        self._ha_climate_service(ent, "set_hvac_mode", {"hvac_mode": mode})
                    except Exception:
                        pass
                    temp_ok = self._ha_climate_service(ent, "set_temperature", {"temperature": float(target_send)})
                    if temp_ok:
                        rb = self._ha_climate_target(ent)
                        try:
                            tid = str(t.get("id"))
                            with self.lock:
                                rt = self.rt.setdefault(tid, {})
                                th = rt.setdefault("THERM", {})
                                if rb is not None:
                                    th["REAL_TARGET_READ"] = float(rb)
                        except Exception:
                            pass
                        if rb is not None and abs(float(rb) - float(target_send)) > 0.3:
                            temp_ok = False
                if not temp_ok:
                    # Fallback through generic climate service path.
                    temp_ok = self._ha_service_call("climate", "set_temperature", {"entity_id": ent, "temperature": float(target_send)})
                    if temp_ok:
                        rb = self._ha_climate_target(ent)
                        try:
                            tid = str(t.get("id"))
                            with self.lock:
                                rt = self.rt.setdefault(tid, {})
                                th = rt.setdefault("THERM", {})
                                if rb is not None:
                                    th["REAL_TARGET_READ"] = float(rb)
                        except Exception:
                            pass
                        if rb is not None and abs(float(rb) - float(target_send)) > 0.3:
                            temp_ok = False
                if not temp_ok:
                    # Last fallback: integer step only.
                    temp_ok = self._ha_climate_service(ent, "set_temperature", {"temperature": float(round(target_send))})
                    if temp_ok:
                        rb2 = self._ha_climate_target(ent)
                        try:
                            tid = str(t.get("id"))
                            with self.lock:
                                rt = self.rt.setdefault(tid, {})
                                th = rt.setdefault("THERM", {})
                                if rb2 is not None:
                                    th["REAL_TARGET_READ"] = float(rb2)
                        except Exception:
                            pass
                        if rb2 is not None and abs(float(rb2) - float(round(target_send))) > 0.3:
                            temp_ok = False
                if temp_ok:
                    st["last_target"] = float(target_send)
                    st["last_temp_ts"] = now
            else:
                temp_ok = True

        if mode_ok or temp_ok:
            self._real_target_last[key_state] = "ON"
            if old != "ON":
                self._real_target_last[key_ts] = now

    def _real_targets_for(self, t: Dict[str, Any], season_key: Optional[str] = None) -> Dict[str, Any]:
        rt = t.get("real_targets") if isinstance(t.get("real_targets"), dict) else {}
        if not isinstance(rt, dict):
            rt = {}
        out: Dict[str, Any] = {}
        try:
            # base/default keys
            out.update(rt)
            if season_key and isinstance(rt.get(season_key), dict):
                out.update(rt.get(season_key) or {})
        except Exception:
            pass
        return out

    def _split_entities(self, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        s = str(v).strip()
        if not s:
            return []
        parts = []
        for raw in s.replace(";", ",").split(","):
            raw = raw.strip()
            if not raw:
                continue
            parts.append(raw)
        return [p for p in parts if p]

    def _apply_real_switch(self, entity_id: str, on: bool) -> None:
        ent = str(entity_id or "").strip()
        if not ent.startswith("switch."):
            return
        desired = "ON" if on else "OFF"
        cache_key = f"sw:{ent}"
        if str(self._real_target_last.get(cache_key) or "") == desired:
            return
        ok = self._ha_service_call("switch", "turn_on" if on else "turn_off", {"entity_id": ent})
        if ok:
            # Fast path: avoid blocking readback GET per entity.
            self._real_target_last[cache_key] = desired
        else:
            self._real_target_last.pop(cache_key, None)

    def _apply_real_switches(self, entities: Any, on: bool) -> None:
        for ent in self._split_entities(entities):
            self._apply_real_switch(ent, on)

    def _sync_real_power_switch_status(self, t: Dict[str, Any], entities: Any) -> None:
        states: List[bool] = []
        for ent in self._split_entities(entities):
            e = str(ent or "").strip()
            if not e.startswith("switch."):
                continue
            st = self._ha_api_request("GET", f"/states/{e}")
            if not isinstance(st, dict):
                continue
            s = str(st.get("state") or "").strip().lower()
            if s == "on":
                states.append(True)
            elif s == "off":
                states.append(False)
        if not states:
            return
        real_state = "ON" if any(states) else "OFF"
        try:
            tid = str(t.get("id") or "")
            with self.lock:
                rt = self.rt.setdefault(tid, {})
                th = rt.setdefault("THERM", {})
                th["REAL_POWER_SWITCH_STATE"] = real_state
        except Exception:
            pass

    def _apply_real_pwm_light(self, entity_id: str, pwm_value: int) -> None:
        ent = str(entity_id or "").strip()
        if not ent.startswith("light."):
            return
        pwm = int(max(0, min(100, int(pwm_value))))
        cache_key = f"li:{ent}"
        if int(self._real_target_last.get(cache_key, -1)) == pwm:
            return
        if pwm <= 0:
            ok = self._ha_service_call("light", "turn_off", {"entity_id": ent})
        else:
            # Keep exact PWM percentage semantics (0..100).
            ok = self._ha_service_call("light", "turn_on", {"entity_id": ent, "brightness_pct": pwm})
        if ok:
            self._real_target_last[cache_key] = pwm

    def _apply_real_pwm_lights(self, entities: Any, pwm_value: int) -> None:
        for ent in self._split_entities(entities):
            self._apply_real_pwm_light(ent, pwm_value)

    def _apply_real_outputs(self, t: Dict[str, Any], desired: Dict[str, Any], outputs: Dict[str, Any], season_key: Optional[str] = None) -> None:
        targets = self._real_targets_for(t, season_key)
        if not isinstance(targets, dict):
            return

        if outputs.get("power"):
            pwm = int(desired.get("power", 0) or 0)
            pwm_light = (
                targets.get("power_light")
                or targets.get("pwm_light")
                or targets.get("dimmer_light")
                or ""
            )
            if pwm_light:
                self._apply_real_pwm_lights(pwm_light, pwm)
            power_switch = (
                targets.get("power_switch")
                or targets.get("relay_switch")
                or targets.get("switch")
                or ""
            )
            if power_switch:
                self._apply_real_switches(power_switch, pwm > 0)
                self._sync_real_power_switch_status(t, power_switch)

        if outputs.get("fan3"):
            fan = desired.get("fan") if isinstance(desired.get("fan"), dict) else {}
            fan_sw = targets.get("fan_switches") if isinstance(targets.get("fan_switches"), dict) else {}
            tid = str(t.get("id") or "")
            # Keep one hold state per thermostat (not per season) so
            # transient WIN/SUM flaps cannot bypass anti-blink protection.
            stage_key = f"fan_stage:{tid}"
            stage_ts_key = f"{stage_key}:ts"
            desired_stage = "off"
            if str((fan or {}).get("max", "OFF")).upper() == "ON":
                desired_stage = "max"
            elif str((fan or {}).get("med", "OFF")).upper() == "ON":
                desired_stage = "med"
            elif str((fan or {}).get("min", "OFF")).upper() == "ON":
                desired_stage = "min"

            effective_stage = desired_stage
            if not bool(self.real_fan_strict_mirror):
                try:
                    hold = int(max(0, int(self.real_fan_min_hold_sec)))
                except Exception:
                    hold = 0
                if hold > 0:
                    last_stage = str(self._real_target_last.get(stage_key) or "")
                    try:
                        last_ts = float(self._real_target_last.get(stage_ts_key, 0.0) or 0.0)
                    except Exception:
                        last_ts = 0.0
                    now = time.time()
                    if last_stage and desired_stage != last_stage and (now - last_ts) < float(hold):
                        effective_stage = last_stage

            # Build a single desired state per physical entity to avoid
            # ON/OFF ping-pong in the same cycle when an entity is mapped
            # to multiple speed buckets (misconfig or legacy merged values).
            entity_on: Dict[str, bool] = {}
            for sp in ("min", "med", "max"):
                ent = (
                    fan_sw.get(sp)
                    or targets.get(f"fan_{sp}_switch")
                    or ""
                )
                if not ent:
                    continue
                sp_on = (sp == effective_stage)
                for e in self._split_entities(ent):
                    ek = str(e).strip()
                    if not ek:
                        continue
                    prev = bool(entity_on.get(ek, False))
                    entity_on[ek] = bool(prev or sp_on)
            for ek, on in entity_on.items():
                self._apply_real_switch(ek, bool(on))
            try:
                prev_stage = str(self._real_target_last.get(stage_key) or "")
                if prev_stage != effective_stage:
                    self._real_target_last[stage_key] = effective_stage
                    self._real_target_last[stage_ts_key] = time.time()
            except Exception:
                pass

    def _apply_real_valve(self, t: Dict[str, Any], valv_on: bool) -> None:
        targets = self._real_targets_for(t, None)
        if not isinstance(targets, dict):
            return
        ent = targets.get("valve_switch") or targets.get("valv_switch") or ""
        if ent:
            self._apply_real_switches(ent, bool(valv_on))

    def _apply_real_valves(self, t: Dict[str, Any], low_on: bool, hot_on: bool) -> None:
        targets = self._real_targets_for(t, None)
        if not isinstance(targets, dict):
            return
        ent_low = (
            targets.get("valve_switch_low")
            or targets.get("valv_switch_low")
            or targets.get("valve_switch_bassa")
            or ""
        )
        ent_hot = (
            targets.get("valve_switch_hot")
            or targets.get("valv_switch_hot")
            or targets.get("valve_switch_alta")
            or ""
        )
        if ent_low:
            self._apply_real_switches(ent_low, bool(low_on))
        if ent_hot:
            self._apply_real_switches(ent_hot, bool(hot_on))
        # Back-compat: drive single valve if configured
        ent = targets.get("valve_switch") or targets.get("valv_switch") or ""
        if ent:
            self._apply_real_switches(ent, bool(low_on or hot_on))

    def _reserved_real_switch_entities(self) -> set:
        """Return switch entities owned by thermostat real_targets (fan/valves).

        These entities should not be overridden by shared/global writers
        (e.g. consensus groups), otherwise relays can oscillate.
        """
        out: set[str] = set()
        try:
            therms = self.therm_list()
        except Exception:
            therms = []
        for t in therms:
            if not isinstance(t, dict):
                continue
            # Collect base + seasonal target blocks if present.
            blocks = [self._real_targets_for(t, None), self._real_targets_for(t, "heat"), self._real_targets_for(t, "cool")]
            for targets in blocks:
                if not isinstance(targets, dict):
                    continue
                fan_sw = targets.get("fan_switches") if isinstance(targets.get("fan_switches"), dict) else {}
                for sp in ("min", "med", "max"):
                    ent = fan_sw.get(sp) or targets.get(f"fan_{sp}_switch") or ""
                    for e in self._split_entities(ent):
                        ek = str(e).strip().lower()
                        if ek.startswith("switch."):
                            out.add(ek)
                for k in (
                    "valve_switch",
                    "valv_switch",
                    "valve_switch_low",
                    "valv_switch_low",
                    "valve_switch_bassa",
                    "valve_switch_hot",
                    "valv_switch_hot",
                    "valve_switch_alta",
                ):
                    for e in self._split_entities(targets.get(k) or ""):
                        ek = str(e).strip().lower()
                        if ek.startswith("switch."):
                            out.add(ek)
        return out

    def _discovery_topics_for_therm(self, tid: str, outputs: Dict[str, Any]) -> List[str]:
        base = "homeassistant"
        topics = [
            f"{base}/climate/e_therm_{tid}_climate/config",
            f"{base}/climate/e_therm_{tid}_climate_v2/config",
            f"{base}/climate/e_therm_{tid}_climate_v3/config",
            f"{base}/climate/e_therm_{tid}_climate_v4/config",
            f"{base}/sensor/e_therm_{tid}_humidity/config",
            f"{base}/switch/e_therm_{tid}_valv/config",
            f"{base}/switch/e_therm_{tid}_valv_hot/config",
            f"{base}/switch/e_therm_{tid}_valv_low/config",
        ]
        if outputs.get("power"):
            topics.append(f"{base}/number/e_therm_{tid}_power/config")
        if outputs.get("fan3"):
            topics.append(f"{base}/switch/e_therm_{tid}_fan_min/config")
            topics.append(f"{base}/switch/e_therm_{tid}_fan_med/config")
            topics.append(f"{base}/switch/e_therm_{tid}_fan_max/config")
        return topics

    def _discovery_topics_for_any(self, t: Dict[str, Any]) -> List[str]:
        tid = str(t.get("id"))
        if self._is_split_outputs(t):
            topics = self._discovery_topics_for_therm_split(
                tid,
                t.get("outputs_heat") or {},
                t.get("outputs_cool") or {},
            )
        else:
            topics = self._discovery_topics_for_therm(tid, (t.get("outputs") or {}))
        for name_slug in self._entity_name_slugs_for_cleanup(t):
            topics.append(f"homeassistant/climate/e_therm_{name_slug}_climate/config")
        return topics

    def _entity_name_slugs_for_cleanup(self, t: Dict[str, Any]) -> List[str]:
        tid = str((t or {}).get("id") or "").strip()
        fallback = f"thermostat_{tid}" if tid else "unknown"
        raw_names = [
            (t or {}).get("name"),
            (t or {}).get("DES"),
            f"thermostat_{tid}" if tid else "",
        ]
        out: List[str] = []
        for raw in raw_names:
            s = str(raw or "").strip()
            if not s:
                continue
            variants = [s]
            low = s.lower()
            for prefix in ("e-therm ", "e_therm ", "etherm "):
                if low.startswith(prefix):
                    variants.append(s[len(prefix):].strip())
            for v in variants:
                slug = _entity_safe_name(v, fallback)
                if slug and slug not in out:
                    out.append(slug)
        return out

    def _is_split_outputs(self, t: Dict[str, Any]) -> bool:
        try:
            return isinstance(t.get("outputs_heat"), dict) or isinstance(t.get("outputs_cool"), dict)
        except Exception:
            return False

    def _season_key_from_act_sea(self, act_sea: Any) -> str:
        return "cool" if str(act_sea or "").upper() == "SUM" else "heat"

    def _outputs_for_season(self, t: Dict[str, Any], season_key: str) -> Dict[str, Any]:
        if str(season_key) == "heat" and "heat" not in self._therm_allowed_modes(t):
            return {}
        if str(season_key) == "cool" and "cool" not in self._therm_allowed_modes(t):
            return {}
        if not self._is_split_outputs(t):
            return t.get("outputs") or {}
        if str(season_key) == "cool":
            return t.get("outputs_cool") or {}
        return t.get("outputs_heat") or {}

    def _discovery_topics_for_therm_split(self, tid: str, heat_out: Dict[str, Any], cool_out: Dict[str, Any]) -> List[str]:
        base = "homeassistant"
        topics = [
            f"{base}/climate/e_therm_{tid}_climate/config",
            f"{base}/climate/e_therm_{tid}_climate_v2/config",
            f"{base}/climate/e_therm_{tid}_climate_v3/config",
            f"{base}/climate/e_therm_{tid}_climate_v4/config",
            f"{base}/sensor/e_therm_{tid}_humidity/config",
            f"{base}/switch/e_therm_{tid}_valv/config",
            f"{base}/switch/e_therm_{tid}_valv_hot/config",
            f"{base}/switch/e_therm_{tid}_valv_low/config",
        ]
        if heat_out.get("power"):
            topics.append(f"{base}/number/e_therm_{tid}_heat_power/config")
        if heat_out.get("fan3"):
            topics.append(f"{base}/switch/e_therm_{tid}_heat_fan_min/config")
            topics.append(f"{base}/switch/e_therm_{tid}_heat_fan_med/config")
            topics.append(f"{base}/switch/e_therm_{tid}_heat_fan_max/config")
        if cool_out.get("power"):
            topics.append(f"{base}/number/e_therm_{tid}_cool_power/config")
        if cool_out.get("fan3"):
            topics.append(f"{base}/switch/e_therm_{tid}_cool_fan_min/config")
            topics.append(f"{base}/switch/e_therm_{tid}_cool_fan_med/config")
            topics.append(f"{base}/switch/e_therm_{tid}_cool_fan_max/config")
        return topics

    def _discovery_topics_for_group(self, g_key: str) -> List[str]:
        base = "homeassistant"
        return [
            f"{base}/switch/e_therm_pdc_group_{g_key}/config",
            f"{base}/switch/e_therm_pdc_group_{g_key}_heat/config",
            f"{base}/switch/e_therm_pdc_group_{g_key}_cool/config",
        ]

    def _cleanup_discovery_topics(self, topics: List[str]) -> None:
        # Publish empty retained payload to remove MQTT Discovery entities from Home Assistant.
        if not topics:
            return
        uniq = [t for t in sorted(set(topics)) if isinstance(t, str) and t.strip()]
        if not uniq:
            return
        if not self._mqtt_connected:
            self._pending_discovery_cleanup.extend(uniq)
            return
        for tp in uniq:
            try:
                self.mqtt.publish(tp, payload="", retain=True)
            except Exception:
                continue

    def _remember_discovery_topic(self, tid: str, topic: str) -> None:
        try:
            key = str(tid)
            tp = str(topic or "").strip()
            if not key or not tp:
                return
            rec = self.runtime.get("published_discovery")
            if not isinstance(rec, dict):
                rec = {}
            arr = rec.get(key)
            if not isinstance(arr, list):
                arr = []
            if tp not in arr:
                arr.append(tp)
            rec[key] = arr
            self.runtime["published_discovery"] = rec
        except Exception:
            pass

    def _published_discovery_topics_for(self, tid: str) -> List[str]:
        try:
            rec = self.runtime.get("published_discovery")
            arr = rec.get(str(tid)) if isinstance(rec, dict) else []
            if isinstance(arr, list):
                return [str(x) for x in arr if str(x or "").strip()]
        except Exception:
            pass
        return []

    def _current_climate_unique_ids(self) -> set[str]:
        out: set[str] = set()
        try:
            for t in self.therm_list():
                if self._display_only_for(t):
                    continue
                tid = str(t.get("id"))
                name = t.get("name") or f"e-Therm {tid}"
                name_slug = _entity_safe_name(name, f"thermostat_{tid}")
                out.add(f"e_therm_{name_slug}_climate")
        except Exception:
            pass
        return out

    def _handle_discovery_config_message(self, topic: str, payload_raw: str) -> bool:
        if not topic.startswith("homeassistant/climate/") or not topic.endswith("/config"):
            return False
        try:
            cfg = json.loads(payload_raw or "{}")
        except Exception:
            return True
        if not isinstance(cfg, dict):
            return True
        uid = str(cfg.get("unique_id") or "").strip()
        if not (uid.startswith("e_therm_") and uid.endswith("_climate")):
            return True
        dev = cfg.get("device") if isinstance(cfg.get("device"), dict) else {}
        model = str(dev.get("model") or "").strip()
        manufacturer = str(dev.get("manufacturer") or "").strip()
        if model != "e-Therm Plus KS" and manufacturer != "Ekonex":
            return True
        if uid in self._current_climate_unique_ids():
            return True
        try:
            self.mqtt.publish(topic, payload="", retain=True)
            print(f"[MQTT] cleaned orphan climate discovery {topic} ({uid})")
        except Exception:
            pass
        return True

    def _cleanup_retained_topics(self, topics: List[str]) -> None:
        if not topics:
            return
        uniq = [t for t in sorted(set(topics)) if isinstance(t, str) and t.strip()]
        if not uniq:
            return
        if not self._mqtt_connected:
            return
        for tp in uniq:
            try:
                self.mqtt.publish(tp, payload="", retain=True)
            except Exception:
                continue

    def _retained_topics_for_removed_therm(self, t: Dict[str, Any]) -> List[str]:
        tid = str((t or {}).get("id") or "").strip()
        if not tid:
            return []
        name = str((t or {}).get("name") or f"vTherm_{tid}")
        name_slug = _topic_safe_name(name)
        topics = [
            f"{self.out_prefix}/thermostats/{tid}/current_temperature",
            f"{self.out_prefix}/thermostats/{tid}/humidity",
            f"{self.out_prefix}/thermostats/{tid}/target_temperature",
            f"{self.out_prefix}/thermostats/{tid}/mode",
            f"{self.out_prefix}/thermostats/{tid}/preset_mode",
            f"{self.out_prefix}/thermostats/{tid}/target_temperature/set",
            f"{self.out_prefix}/thermostats/{tid}/mode/set",
            f"{self.out_prefix}/thermostats/{tid}/preset_mode/set",
            f"{self.out_prefix}/thermostats/{tid}/power",
            f"{self.out_prefix}/thermostats/{tid}/power/set",
            f"{self.out_prefix}/thermostats/{tid}/heat/power",
            f"{self.out_prefix}/thermostats/{tid}/heat/power/set",
            f"{self.out_prefix}/thermostats/{tid}/cool/power",
            f"{self.out_prefix}/thermostats/{tid}/cool/power/set",
            f"{self.out_prefix}/thermostats/{tid}/valv/state",
            f"{self.out_prefix}/thermostats/{tid}/valv_hot/state",
            f"{self.out_prefix}/thermostats/{tid}/valv_low/state",
            f"{self.out_prefix}/valv/{tid}/state",
            f"{self.out_prefix}/valv/{tid}/set",
            f"{self.out_prefix}/valv_hot/{tid}/state",
            f"{self.out_prefix}/valv_hot/{tid}/set",
            f"{self.out_prefix}/valv_low/{tid}/state",
            f"{self.out_prefix}/valv_low/{tid}/set",
        ]
        for sp in ("min", "med", "max"):
            topics.extend([
                f"{self.out_prefix}/thermostats/{tid}/fan/{sp}",
                f"{self.out_prefix}/thermostats/{tid}/fan/{sp}/set",
                f"{self.out_prefix}/thermostats/{tid}/heat/fan/{sp}",
                f"{self.out_prefix}/thermostats/{tid}/heat/fan/{sp}/set",
                f"{self.out_prefix}/thermostats/{tid}/cool/fan/{sp}",
                f"{self.out_prefix}/thermostats/{tid}/cool/fan/{sp}/set",
            ])
        if name_slug:
            topics.extend([
                f"{self.out_prefix}/thermostats/{name_slug}/valv/state",
                f"{self.out_prefix}/thermostats/{name_slug}/valv/set",
                f"{self.out_prefix}/thermostats/{name_slug}/valv_hot/state",
                f"{self.out_prefix}/thermostats/{name_slug}/valv_low/state",
            ])
        return topics

    def _discovery_topics_full_cleanup(self, max_tid: int = 128) -> List[str]:
        topics: List[str] = []
        # Conservative superset for legacy/current thermostat discovery topics.
        for n in range(1, max(1, int(max_tid)) + 1):
            tid = str(n)
            topics.extend(self._discovery_topics_for_therm(tid, {"power": True, "fan3": True}))
            topics.extend(self._discovery_topics_for_therm_split(
                tid,
                {"power": True, "fan3": True},
                {"power": True, "fan3": True},
            ))
        # PDC + known group topics.
        topics.extend(self._discovery_topics_for_group("ha"))
        topics.extend([
            "homeassistant/switch/e_therm_pdc/config",
            "homeassistant/switch/e_therm_pdc_heat/config",
            "homeassistant/switch/e_therm_pdc_cool/config",
            "homeassistant/switch/e_therm_pdc_ha/config",
            "homeassistant/switch/e_therm_pdc_ha_heat/config",
            "homeassistant/switch/e_therm_pdc_ha_cool/config",
        ])
        cfg_groups = self.cfg.get("consensus_groups") if isinstance(self.cfg, dict) else []
        if isinstance(cfg_groups, list):
            for g in cfg_groups:
                if not isinstance(g, dict):
                    continue
                gk = str(g.get("key") or "").strip()
                if not gk:
                    continue
                topics.extend(self._discovery_topics_for_group(gk))
        return [t for t in sorted(set(topics)) if isinstance(t, str) and t.strip()]

    def apply_config(self, cfg: Dict[str, Any]):
        old = self.cfg or {}
        old_therms = old.get("thermostats") or []
        old_by_id = {
            str(t.get("id")): t
            for t in old_therms
            if isinstance(t, dict) and t.get("id") is not None
        }
        new_therms = (cfg or {}).get("thermostats") or []
        new_by_id = {
            str(t.get("id")): t
            for t in new_therms
            if isinstance(t, dict) and t.get("id") is not None
        }
        try:
            snap = self.state.snapshot()
            for e in snap.get("entities") or []:
                if str(e.get("type") or "").lower() != "thermostats":
                    continue
                tid0 = str(e.get("id") or "").strip()
                if not tid0 or tid0 in new_by_id or tid0 in old_by_id:
                    continue
                st0 = e.get("static") if isinstance(e.get("static"), dict) else {}
                rt0 = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
                old_by_id[tid0] = {
                    "id": tid0,
                    "name": e.get("name") or st0.get("DES") or rt0.get("DES") or f"vTherm_{tid0}",
                    "outputs": {"power": True, "fan3": True},
                    "outputs_heat": {"power": True, "fan3": True},
                    "outputs_cool": {"power": True, "fan3": True},
                }
        except Exception:
            pass

        # Cleanup discovery for removed thermostats or removed outputs.
        to_cleanup: List[str] = []
        retained_cleanup: List[str] = []
        removed_ids: List[str] = []
        for tid, old_t in old_by_id.items():
            if tid not in new_by_id:
                removed_ids.append(str(tid))
                to_cleanup.extend(self._discovery_topics_for_any(old_t))
                to_cleanup.extend(self._published_discovery_topics_for(tid))
                retained_cleanup.extend(self._retained_topics_for_removed_therm(old_t))
                if self._is_split_outputs(old_t):
                    to_cleanup.extend(
                        self._discovery_topics_for_therm_split(
                            tid,
                            old_t.get("outputs_heat") or {},
                            old_t.get("outputs_cool") or {},
                        )
                    )
                else:
                    to_cleanup.extend(self._discovery_topics_for_therm(tid, (old_t.get("outputs") or {})))
                continue
            new_t = new_by_id.get(tid) or {}
            if self._display_only_for(new_t):
                to_cleanup.extend(self._discovery_topics_for_any(old_t))
                to_cleanup.extend(self._published_discovery_topics_for(tid))
                continue
            old_split = self._is_split_outputs(old_t)
            new_split = self._is_split_outputs(new_t)
            if old_split or new_split:
                # If switching modes or disabling some seasonal outputs: cleanup the whole old set then republish.
                to_cleanup.extend(
                    self._discovery_topics_for_therm_split(
                        tid,
                        old_t.get("outputs_heat") or {},
                        old_t.get("outputs_cool") or {},
                    )
                )
                to_cleanup.extend(self._discovery_topics_for_therm(tid, (old_t.get("outputs") or {})))
            else:
                old_out = old_t.get("outputs") or {}
                new_out = new_t.get("outputs") or {}
                if old_out.get("power") and not new_out.get("power"):
                    to_cleanup.append(f"homeassistant/number/e_therm_{tid}_power/config")
                if old_out.get("fan3") and not new_out.get("fan3"):
                    to_cleanup.append(f"homeassistant/switch/e_therm_{tid}_fan_min/config")
                    to_cleanup.append(f"homeassistant/switch/e_therm_{tid}_fan_med/config")
                    to_cleanup.append(f"homeassistant/switch/e_therm_{tid}_fan_max/config")
        if to_cleanup:
            self._cleanup_discovery_topics(to_cleanup)
        if retained_cleanup:
            self._cleanup_retained_topics(retained_cleanup)

        # Cleanup discovery for removed consensus groups.
        try:
            old_group_keys: set[str] = set()
            new_group_keys: set[str] = set()

            def _collect(keys: set[str], cfg_obj: Dict[str, Any]) -> None:
                # from consensus_groups config
                groups = cfg_obj.get("consensus_groups") if isinstance(cfg_obj, dict) else []
                if isinstance(groups, list):
                    for g in groups:
                        if not isinstance(g, dict):
                            continue
                        name = str(g.get("name") or "").strip()
                        if not name:
                            continue
                        keys.add(_topic_safe_name(name).lower())
                # from thermostat consensus_group fields
                therms = cfg_obj.get("thermostats") if isinstance(cfg_obj, dict) else []
                if isinstance(therms, list):
                    for t in therms:
                        if not isinstance(t, dict):
                            continue
                        for name in [
                            str(t.get("consensus_group_heat") or t.get("consensus_group") or t.get("pdc_group") or "").strip(),
                            str(t.get("consensus_group_cool") or t.get("consensus_group") or t.get("pdc_group") or "").strip(),
                        ]:
                            if not name:
                                continue
                            keys.add(_topic_safe_name(name).lower())

            _collect(old_group_keys, old)
            _collect(new_group_keys, cfg or {})

            removed = sorted(k for k in old_group_keys if k not in new_group_keys)
            grp_cleanup: List[str] = []
            for gk in removed:
                grp_cleanup.extend(self._discovery_topics_for_group(gk))
            if grp_cleanup:
                self._cleanup_discovery_topics(grp_cleanup)
        except Exception:
            pass

        with self.lock:
            self.cfg = cfg or {}
            for tid in removed_ids:
                self.rt.pop(str(tid), None)
                self.desired.pop(str(tid), None)
                self.therm_static.pop(str(tid), None)
                self._demand_latch.pop(f"{tid}:single", None)
                self._demand_latch.pop(f"{tid}:heat", None)
                self._demand_latch.pop(f"{tid}:cool", None)
                self._pwm.pop(str(tid), None)
                self._manual_override_until.pop(str(tid), None)
                self._manual_valve_until.pop(str(tid), None)
                self._manual_valve_state.pop(str(tid), None)
                self._ha_bridge_mode_hold.pop(str(tid), None)
                self._ha_bridge_setpoint_hold.pop(str(tid), None)
                rec = self.runtime.get("published_discovery")
                if isinstance(rec, dict):
                    rec.pop(str(tid), None)
                    self.runtime["published_discovery"] = rec
            self.runtime["rt_cache"] = self.rt
            self.runtime["desired"] = self.desired
            save_config(self.cfg)
            try:
                save_runtime(self.runtime)
            except Exception:
                pass
        try:
            self.state.set_meta("vtherm_config", self.cfg)
        except Exception:
            pass
        for tid in removed_ids:
            try:
                self.state.remove_entity("thermostats", tid)
            except Exception:
                pass
        self._sync_ui()
        # Config saves may cleanup old retained discovery topics. Republish
        # immediately so HA does not temporarily lose the MQTT entities.
        self._publish_discovery()

    # -------------------- MQTT connect --------------------

    def connect(self):
        host, port = self._mqtt_target()
        try:
            self.mqtt.connect(host, port, 60)
            self.mqtt.loop_start()
        except Exception as e:
            print(f"[WARN] MQTT connect failed to {host}:{port} -> {e}")
            try:
                self.mqtt.loop_start()
            except Exception:
                pass

    def _reconnect_mqtt(self, reason: str) -> None:
        """Best-effort reconnect without restarting the add-on."""
        now = time.time()
        with self._mqtt_manage_lock:
            if self._mqtt_reconnecting:
                return
            # Backoff to avoid thrashing
            min_gap = float(self._reconnect_backoff_sec or 5.0)
            if (now - float(self._last_reconnect_attempt_ts or 0.0)) < min_gap:
                return
            self._mqtt_reconnecting = True
            self._last_reconnect_attempt_ts = now
            self._last_reconnect_reason = str(reason or "").strip()

        try:
            host, port = self._mqtt_target()
            print(f"[WATCHDOG] MQTT reconnect: {host}:{port} reason={self._last_reconnect_reason}")
            try:
                self._log_event(
                    origin="system",
                    tid=None,
                    name=None,
                    source_num=None,
                    category="mqtt",
                    field="reconnect",
                    msg=f"reason={self._last_reconnect_reason}",
                    extra={"host": host, "port": port, "backoff_sec": float(self._reconnect_backoff_sec)},
                )
            except Exception:
                pass
            old = self.mqtt

            # Avoid blocking the watchdog thread if paho's internal thread is stuck.
            def _best_effort_stop(c: mqtt.Client) -> None:
                try:
                    c.disconnect()
                except Exception:
                    pass
                try:
                    c.loop_stop(force=True)
                except Exception:
                    pass

            try:
                th = threading.Thread(target=_best_effort_stop, args=(old,), daemon=True)
                th.start()
                th.join(2.0)
            except Exception:
                pass

            # Recreate client to recover from stuck network loop scenarios.
            try:
                self.mqtt = self._create_mqtt_client()
            except Exception:
                # As a fallback, keep the previous instance.
                pass

            try:
                self.mqtt.connect(host, port, 60)
            except Exception as e:
                print(f"[WATCHDOG] MQTT reconnect connect() failed: {e}")

            try:
                self.mqtt.loop_start()
            except Exception as e:
                print(f"[WATCHDOG] MQTT reconnect loop_start() failed: {e}")

            # Increase backoff gradually (max 60s). Reset on first message/connect.
            self._reconnect_backoff_sec = float(min(60.0, max(5.0, self._reconnect_backoff_sec * 1.6)))
        finally:
            with self._mqtt_manage_lock:
                self._mqtt_reconnecting = False

    def start_watchdog(self) -> None:
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="watchdog_loop", daemon=True)
        self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        # Conservative watchdog: only intervenes on clear stuck conditions.
        while True:
            try:
                self._watchdog_step()
            except Exception:
                pass
            time.sleep(self._opt_seconds("watchdog_interval_sec", 10.0, 5.0))

    def _watchdog_step(self) -> None:
        enabled = bool(self.opts.get("watchdog_enabled", True))
        if not enabled:
            return

        now = time.time()
        cfg_has_therms = bool(self.therm_list())
        stale_sec = int(self.opts.get("watchdog_stale_sec", 120) or 120)
        stale_sec = int(max(30, stale_sec))

        # Publish lightweight health for UI/diagnostics.
        try:
            last_any = float(self._last_mqtt_any_ts or 0.0)
            last_src = float(self._last_source_ts or 0.0)
            last_ctl = float(self.runtime.get("_last_control_ts", 0.0) or 0.0)
            health = {
                "mqtt_connected": bool(self._mqtt_connected),
                "mqtt_last_any_age_sec": (now - last_any) if last_any else None,
                "mqtt_last_source_age_sec": (now - last_src) if last_src else None,
                "control_last_age_sec": (now - last_ctl) if last_ctl else None,
                "control_thread_alive": bool(self._control_thread and self._control_thread.is_alive()),
                "watchdog_backoff_sec": float(self._reconnect_backoff_sec),
                "last_reconnect_reason": self._last_reconnect_reason,
                "last_mqtt_error": str(self._last_mqtt_error or ""),
            }
            self.state.set_meta("health", health)
        except Exception:
            pass

        # Emit ACK timeouts even if no new source messages arrive.
        try:
            now2 = time.time()
            timeout = float(self.log_ack_timeout_sec)
            to_del = []
            for k, ack in list(self._pending_acks.items()):
                try:
                    ts0 = float((ack or {}).get("ts") or 0.0)
                except Exception:
                    ts0 = 0.0
                if ts0 and (now2 - ts0) > timeout:
                    to_del.append(k)
            for k in to_del:
                ack = self._pending_acks.pop(k, None) or {}
                try:
                    if self._log_enabled("MIN"):
                        self._log_event(
                            origin="system",
                            tid=str(k.split(":", 1)[0]),
                            name=None,
                            source_num=None,
                            category="ack",
                            field=f"{k.split(':',1)[1]}.timeout",
                            old=ack.get("expected"),
                            new=None,
                            msg=f"ACK timeout (origin={ack.get('origin')})",
                        )
                except Exception:
                    pass
        except Exception:
            pass

        # Ensure control thread stays alive.
        if not (self._control_thread and self._control_thread.is_alive()):
            print("[WATCHDOG] control thread was not alive -> restarting control loop")
            try:
                self._log_event(
                    origin="system",
                    tid=None,
                    name=None,
                    source_num=None,
                    category="control",
                    field="restart_thread",
                    msg="control thread was not alive -> restart",
                )
            except Exception:
                pass
            self.start_control()

        # Keep HA climate-backed thermostats refreshed even without MQTT source events.
        try:
            self._poll_ha_climate_states()
        except Exception:
            pass
        try:
            self._poll_ha_multi_sensor_states()
        except Exception:
            pass
        try:
            self._poll_ha_sensor_states()
        except Exception:
            pass
        try:
            self._sync_virtual_states()
        except Exception:
            pass

        # If MQTT reports disconnected, attempt reconnect with backoff.
        if not bool(self._mqtt_connected):
            self._reconnect_mqtt("mqtt_not_connected")
            return

        # Do not force reconnect based on source staleness; reconnect only on real MQTT disconnect.



    def start_control(self) -> None:
        if self._control_thread and self._control_thread.is_alive():
            return
        self._control_thread = threading.Thread(target=self._control_loop, name="control_loop", daemon=True)
        self._control_thread.start()

    def _get_pwm_controller(self, tid: str) -> PWMController:
        c = self._pwm.get(str(tid))
        if c is None:
            c = PWMController(
                kp=self.pwm_kp,
                ki=self.pwm_ki,
                windup=self.pwm_windup,
                min_to_med=self.pwm_min_to_med,
                med_to_max=self.pwm_med_to_max,
            )
            self._pwm[str(tid)] = c
        return c

    def _control_loop(self) -> None:
        # periodic control: compute PWM + map to fan stages
        while True:
            try:
                # Run loop always; enable can be global or per-thermostat.
                self._control_step_all()
            except Exception:
                pass
            time.sleep(1)

    def _control_step_all(self) -> None:
        now = time.time()
        # run every control interval
        last = float(self.runtime.get("_last_control_ts", 0.0) or 0.0)
        if (now - last) < self._opt_seconds("control_interval_sec", 10.0, 5.0):
            return
        self.runtime["_last_control_ts"] = now

        # Keep HA climate-backed thermostats in sync even when watchdog is disabled.
        try:
            self._poll_ha_climate_states()
        except Exception:
            pass
        try:
            self._poll_ha_multi_sensor_states()
        except Exception:
            pass
        try:
            self._poll_ha_sensor_states()
        except Exception:
            pass
        try:
            self._sync_virtual_states()
        except Exception:
            pass

        for t in self.therm_list():
            try:
                if not self._auto_enabled_for(t):
                    try:
                        tid = str(t.get("id"))
                        with self.lock:
                            rt = self.rt.setdefault(tid, {})
                            th = rt.setdefault("THERM", {})
                            th["DEMAND_ON"] = "OFF"
                            th["DEMAND_REASON"] = "AUTO_DISABLED"
                    except Exception:
                        pass
                    continue
                self._control_one(t, now)
            except Exception:
                continue
        # Keep consensus states fresh even if no outputs changed.
        try:
            self._publish_pdc_consensus()
        except Exception:
            pass

        save_interval = self._opt_seconds("runtime_save_min_sec", 30.0, 5.0)
        if (now - float(self._last_control_runtime_save_ts or 0.0)) >= save_interval:
            try:
                save_runtime(self.runtime)
                self._last_control_runtime_save_ts = now
            except Exception:
                pass

    def _control_one(self, t: Dict[str, Any], now: float) -> None:
        tid = str(t.get("id"))
        def _set_real_debug(demand: str, reason: str, adapt_target: Any = None, pwm_value: Any = None) -> None:
            try:
                with self.lock:
                    rt_dbg = self.rt.setdefault(tid, {})
                    th_dbg = rt_dbg.setdefault("THERM", {})
                    demand_s = str(demand or "").upper()
                    th_dbg["DEMAND_ON"] = demand_s
                    th_dbg["OUT_STATUS"] = "ON" if demand_s == "ON" else "OFF"
                    th_dbg["DEMAND_REASON"] = str(reason or "").upper()
                    if pwm_value is not None:
                        th_dbg["PWM"] = int(max(0, min(100, int(pwm_value))))
                    elif demand_s == "OFF":
                        th_dbg["PWM"] = 0
                    if adapt_target is None:
                        th_dbg.pop("ADAPT_TARGET", None)
                    else:
                        th_dbg["ADAPT_TARGET"] = float(adapt_target)
            except Exception:
                pass
        split = self._is_split_outputs(t)
        # use active season outputs when split, otherwise legacy
        with self.lock:
            rt0 = self.rt.get(tid) or {}
            th0 = rt0.get("THERM") if isinstance(rt0.get("THERM"), dict) else {}
            sea0 = th0.get("ACT_SEA") if isinstance(th0, dict) else None
        active_sk = self._season_key_from_act_sea(sea0)
        outputs = self._outputs_for_season(t, active_sk) if split else (t.get("outputs") or {})
        has_real_therm = bool(self._real_thermostat_entity(t))
        if not (outputs.get("power") or outputs.get("fan3") or has_real_therm):
            _set_real_debug("OFF", "NO_OUTPUTS_OR_REAL")
            return

        # manual override window
        ov_key = f"{tid}:{active_sk}" if split else tid
        until = float(self._manual_override_until.get(ov_key, 0.0) or 0.0)
        if until and now < until:
            _set_real_debug("OFF", "MANUAL_OVERRIDE")
            return

        with self.lock:
            rt = self.rt.get(tid) or {}
            th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}

        cur = rt.get("TEMP")
        if cur is None:
            _set_real_debug("OFF", "NO_CURRENT_TEMP")
            return
        cur_f = float(cur)

        sea = str(th.get("ACT_SEA") or "WIN").upper()
        model = str(th.get("ACT_MODEL") or th.get("ACT_MODE") or "").upper()

        # Determine setpoint: prefer live TEMP_THR.VAL
        setp = None
        thr = th.get("TEMP_THR") if isinstance(th.get("TEMP_THR"), dict) else None
        if thr and thr.get("VAL") is not None:
            setp = _as_float(thr.get("VAL"))

        # Fallback: compute from schedule+profiles if present
        if setp is None and model in ("WEEKLY", "AUTO", "SD1", "SD2"):
            st = self._get_therm_static(tid)
            sea_st = st.get("SUM" if sea == "SUM" else "WIN")
            if isinstance(sea_st, dict):
                # choose table
                import datetime as _dt
                dt = _dt.datetime.now()
                hour = int(dt.hour)
                if model in ("SD1", "SD2"):
                    table = model
                else:
                    table = DAYS[int(dt.weekday())]
                arr = sea_st.get(table)
                if isinstance(arr, list) and len(arr) == 24:
                    slot = arr[hour]
                    if isinstance(slot, dict):
                        tt = str(slot.get("T") or "")
                        if tt in ("1", "2", "3"):
                            key = f"T{tt}"
                            setp = _as_float(sea_st.get(key))

        if setp is None:
            _set_real_debug("OFF", "NO_SETPOINT")
            return

        # Thermal error sign normalized as "positive means request active for current season".
        if sea == "SUM":
            err = cur_f - float(setp)
        else:
            err = float(setp) - cur_f

        # Deterministic hysteresis with centered band around setpoint:
        # COOL:
        #   - start ON at setpoint + deadband_on
        #   - keep ON until temperature drops to setpoint - deadband_off
        # HEAT:
        #   - start ON at setpoint - deadband_on
        #   - keep ON until temperature rises to setpoint + deadband_off
        demand_on = False
        demand_reason = "NO_DEMAND"
        if str(model).upper() == "OFF":
            demand_on = False
            demand_reason = "MODEL_OFF"
        else:
            on_thr = float(max(0.0, self.pwm_deadband_on))
            off_thr = float(max(0.0, self.pwm_deadband_off))
            if off_thr > on_thr:
                off_thr = on_thr
            latch_key = f"{tid}:{active_sk if split else 'single'}"
            prev_on = bool(self._demand_latch.get(latch_key, False))
            if sea == "SUM":
                start_thr = float(setp) + on_thr
                stop_thr = float(setp) - off_thr
                if prev_on:
                    demand_on = bool(cur_f > stop_thr)
                    demand_reason = "HYST_HOLD_COOL" if demand_on else "HYST_RELEASE_COOL"
                else:
                    demand_on = bool(cur_f >= start_thr)
                    demand_reason = "HYST_START_COOL" if demand_on else "HYST_WAIT_COOL"
            else:
                start_thr = float(setp) - on_thr
                stop_thr = float(setp) + off_thr
                if prev_on:
                    demand_on = bool(cur_f < stop_thr)
                    demand_reason = "HYST_HOLD_HEAT" if demand_on else "HYST_RELEASE_HEAT"
                else:
                    demand_on = bool(cur_f <= start_thr)
                    demand_reason = "HYST_START_HEAT" if demand_on else "HYST_WAIT_HEAT"
            self._demand_latch[latch_key] = bool(demand_on)

        # PWM is now coherent with hysteresis demand state.
        if not demand_on:
            pwm = 0
        elif float(err) >= float(max(0.0, self.pwm_full_error)):
            pwm = 100
        else:
            c = self._get_pwm_controller(tid)
            if sea == "SUM":
                pwm = c.compute_pwm(cur_f, float(setp), now=now)
            else:
                pwm = c.compute_pwm(float(setp), cur_f, now=now)
            if self.pwm_min_active > 0:
                pwm = max(int(pwm), int(self.pwm_min_active))

        _set_real_debug("ON" if demand_on else "OFF", demand_reason, pwm_value=pwm)

        if has_real_therm:
            try:
                self._apply_real_thermostat_demand(t, bool(demand_on), sea)
            except Exception as ex:
                try:
                    tid = str(t.get("id"))
                    with self.lock:
                        rt_dbg = self.rt.setdefault(tid, {})
                        th_dbg = rt_dbg.setdefault("THERM", {})
                        th_dbg["DEMAND_ON"] = "ON" if bool(demand_on) else "OFF"
                        th_dbg["DEMAND_REASON"] = "BRIDGE_EXCEPTION"
                        th_dbg["BRIDGE_ERROR"] = str(ex)[:180]
                except Exception:
                    pass

        desired = self._get_desired_season(tid, active_sk) if split else self._get_desired(tid)
        prev_power = desired.get("power")
        prev_fan = (desired.get("fan") or {}).copy() if isinstance(desired.get("fan"), dict) else desired.get("fan")
        desired["power"] = int(max(0, min(100, int(pwm))))

        # stage mapping
        if outputs.get("fan3"):
            c = self._get_pwm_controller(tid)
            stage = c.pwm_to_stage(int(desired["power"]))
            fan = desired.get("fan") or {"min": "OFF", "med": "OFF", "max": "OFF"}
            if stage == "MIN":
                fan = {"min": "ON", "med": "OFF", "max": "OFF"}
            elif stage == "MED":
                fan = {"min": "OFF", "med": "ON", "max": "OFF"}
            elif stage == "MAX":
                fan = {"min": "OFF", "med": "OFF", "max": "ON"}
            else:
                fan = {"min": "OFF", "med": "OFF", "max": "OFF"}
            desired["fan"] = fan

        if split:
            self._set_desired_season(tid, active_sk, desired)
            self._publish_outputs_state(t, active_sk)
            try:
                if self._log_enabled("NORMAL"):
                    if (prev_power != desired.get("power")) or (prev_fan != desired.get("fan")):
                        # Throttle: log only on fan stage changes or significant PWM steps, or at most every N seconds.
                        step = int(max(1, self.log_auto_pwm_step))
                        min_sec = int(max(0, self.log_auto_min_sec))
                        last = self.runtime.get("_last_auto_log") if isinstance(self.runtime.get("_last_auto_log"), dict) else {}
                        key = f"{tid}:{active_sk}"
                        rec = last.get(key) if isinstance(last, dict) else None
                        if not isinstance(rec, dict):
                            rec = {}
                        last_ts = float(rec.get("ts") or 0.0)
                        last_pwm = _as_int(rec.get("pwm"))
                        last_stage = str(rec.get("stage") or "")
                        cur_pwm = int(desired.get("power") or 0)
                        cur_stage = ""
                        try:
                            cur_stage = self._get_pwm_controller(tid).pwm_to_stage(cur_pwm)
                        except Exception:
                            cur_stage = ""
                        stage_changed = (last_stage != cur_stage) and bool(cur_stage)
                        pwm_step = (last_pwm is None) or (abs(cur_pwm - int(last_pwm)) >= step)
                        time_ok = (min_sec == 0) or (not last_ts) or ((now - last_ts) >= float(min_sec))
                        if stage_changed or (pwm_step and time_ok):
                            self._log_event(
                                origin="auto",
                                tid=str(tid),
                                name=str(t.get("name") or f"vTherm {tid}"),
                                source_num=int((t.get("source") or {}).get("num")) if (t.get("source") or {}).get("num") is not None else None,
                                category="out",
                                field=f"{active_sk}.auto",
                                old={"power": prev_power, "fan": prev_fan},
                                new={"power": desired.get("power"), "fan": desired.get("fan")},
                                msg=f"cur={cur_f:.2f} set={float(setp):.2f} sea={sea} model={model} stage={cur_stage}",
                                extra={"err": float(err) if model != "OFF" else 0.0},
                            )
                            rec["ts"] = now
                            rec["pwm"] = cur_pwm
                            rec["stage"] = cur_stage
                            if not isinstance(last, dict):
                                last = {}
                            last[key] = rec
                            self.runtime["_last_auto_log"] = last
            except Exception:
                pass
            # force inactive season outputs OFF if configured
            inactive_sk = "cool" if active_sk == "heat" else "heat"
            inactive_outputs = self._outputs_for_season(t, inactive_sk)
            if inactive_outputs.get("power") or inactive_outputs.get("fan3"):
                off = self._desired_defaults()
                self._set_desired_season(tid, inactive_sk, off)
                # Avoid ON->OFF ping-pong when active/inactive seasons share the same
                # physical entities in real_targets.
                apply_real_inactive = True
                try:
                    active_outputs = self._outputs_for_season(t, active_sk)
                    active_targets = self._real_targets_for(t, active_sk)
                    inactive_targets = self._real_targets_for(t, inactive_sk)
                    active_ents = self._real_entities_for_outputs(active_outputs, active_targets)
                    inactive_ents = self._real_entities_for_outputs(inactive_outputs, inactive_targets)
                    if active_ents and inactive_ents and (active_ents & inactive_ents):
                        apply_real_inactive = False
                except Exception:
                    apply_real_inactive = True
                self._publish_outputs_state(t, inactive_sk, apply_real=apply_real_inactive)
                try:
                    self._log_event(
                        origin="auto",
                        tid=str(tid),
                        name=str(t.get("name") or f"vTherm {tid}"),
                        source_num=int((t.get("source") or {}).get("num")) if (t.get("source") or {}).get("num") is not None else None,
                        category="out",
                        field=f"{inactive_sk}.force_off",
                        old=None,
                        new=off,
                        msg="inactive season outputs forced OFF",
                    )
                except Exception:
                    pass
        else:
            self._set_desired(tid, desired)
            self._publish_outputs_state(t)
            try:
                if self._log_enabled("NORMAL"):
                    if (prev_power != desired.get("power")) or (prev_fan != desired.get("fan")):
                        step = int(max(1, self.log_auto_pwm_step))
                        min_sec = int(max(0, self.log_auto_min_sec))
                        last = self.runtime.get("_last_auto_log") if isinstance(self.runtime.get("_last_auto_log"), dict) else {}
                        key = str(tid)
                        rec = last.get(key) if isinstance(last, dict) else None
                        if not isinstance(rec, dict):
                            rec = {}
                        last_ts = float(rec.get("ts") or 0.0)
                        last_pwm = _as_int(rec.get("pwm"))
                        last_stage = str(rec.get("stage") or "")
                        cur_pwm = int(desired.get("power") or 0)
                        cur_stage = ""
                        try:
                            cur_stage = self._get_pwm_controller(tid).pwm_to_stage(cur_pwm)
                        except Exception:
                            cur_stage = ""
                        stage_changed = (last_stage != cur_stage) and bool(cur_stage)
                        pwm_step = (last_pwm is None) or (abs(cur_pwm - int(last_pwm)) >= step)
                        time_ok = (min_sec == 0) or (not last_ts) or ((now - last_ts) >= float(min_sec))
                        if stage_changed or (pwm_step and time_ok):
                            self._log_event(
                                origin="auto",
                                tid=str(tid),
                                name=str(t.get("name") or f"vTherm {tid}"),
                                source_num=int((t.get("source") or {}).get("num")) if (t.get("source") or {}).get("num") is not None else None,
                                category="out",
                                field="auto",
                                old={"power": prev_power, "fan": prev_fan},
                                new={"power": desired.get("power"), "fan": desired.get("fan")},
                                msg=f"cur={cur_f:.2f} set={float(setp):.2f} sea={sea} model={model} stage={cur_stage}",
                                extra={"err": float(err) if model != "OFF" else 0.0},
                            )
                            rec["ts"] = now
                            rec["pwm"] = cur_pwm
                            rec["stage"] = cur_stage
                            if not isinstance(last, dict):
                                last = {}
                            last[key] = rec
                            self.runtime["_last_auto_log"] = last
            except Exception:
                pass
    def _on_disconnect(self, *args, **kwargs):
        client = args[0] if len(args) > 0 else None
        # Ignore callbacks from stale MQTT clients replaced during reconnect.
        if client is not None and client is not self.mqtt:
            return
        rc = args[2] if len(args) > 2 else kwargs.get("rc", 0)
        self._mqtt_connected = False
        try:
            self._last_mqtt_error = f"disconnect rc={rc}"
        except Exception:
            pass
        try:
            self._log_event(
                origin="system",
                tid=None,
                name=None,
                source_num=None,
                category="mqtt",
                field="disconnect",
                old=True,
                new=False,
                msg=f"MQTT disconnected rc={rc}",
            )
        except Exception:
            pass
        # Do not publish retained offline on transient disconnects: this may make
        # HA entities appear unavailable/disappear during short MQTT hiccups.

    def _on_connect(self, *args, **kwargs):
        client = args[0] if len(args) > 0 else None
        # Ignore callbacks from stale MQTT clients replaced during reconnect.
        if client is not None and client is not self.mqtt:
            return
        flags = args[2] if len(args) > 2 else kwargs.get("flags", {})
        rc = args[3] if len(args) > 3 else kwargs.get("rc", 0)
        self._mqtt_connected = True
        try:
            self._reconnect_backoff_sec = 5.0
            self._last_mqtt_any_ts = time.time()
            self._last_mqtt_error = ""
        except Exception:
            pass
        try:
            self._log_event(
                origin="system",
                tid=None,
                name=None,
                source_num=None,
                category="mqtt",
                field="connect",
                old=False,
                new=True,
                msg=f"MQTT connected rc={rc}",
            )
        except Exception:
            pass
        # Run queued discovery cleanup (if any) once connected.
        try:
            if self._pending_discovery_cleanup:
                pending = self._pending_discovery_cleanup
                self._pending_discovery_cleanup = []
                self._cleanup_discovery_topics(pending)
        except Exception:
            pass
        # Source (e-safe)
        if client is None:
            return
        # Clear any retained /set commands before subscribing, to avoid spurious manual overrides.
        self._clear_retained_command_topics()
        client.subscribe(f"{self.source_prefix}/thermostats/+", qos=0)
        client.subscribe(f"{self.source_prefix}/thermostats/+/+", qos=0)

        # Output commands (power/fan)
        client.subscribe(f"{self.out_prefix}/thermostats/+/power/set", qos=0)
        client.subscribe(f"{self.out_prefix}/thermostats/+/fan/+/set", qos=0)
        # Output commands (split heat/cool)
        client.subscribe(f"{self.out_prefix}/thermostats/+/heat/power/set", qos=0)
        client.subscribe(f"{self.out_prefix}/thermostats/+/heat/fan/+/set", qos=0)
        client.subscribe(f"{self.out_prefix}/thermostats/+/cool/power/set", qos=0)
        client.subscribe(f"{self.out_prefix}/thermostats/+/cool/fan/+/set", qos=0)

        # Clone thermostat commands from HA (MQTT climate)
        client.subscribe(f"{self.out_prefix}/thermostats/+/target_temperature/set", qos=0)
        client.subscribe(f"{self.out_prefix}/thermostats/+/mode/set", qos=0)
        client.subscribe(f"{self.out_prefix}/thermostats/+/preset_mode/set", qos=0)
        client.subscribe("homeassistant/climate/+/config", qos=0)
        client.subscribe(f"{self.out_prefix}/valv/+/set", qos=0)
        client.subscribe(f"{self.out_prefix}/valv_hot/+/set", qos=0)
        client.subscribe(f"{self.out_prefix}/valv_low/+/set", qos=0)

        self._sync_ui()
        try:
            self._poll_ha_climate_states(force=True)
        except Exception:
            pass
        try:
            self._poll_ha_multi_sensor_states(force=True)
        except Exception:
            pass
        try:
            self._poll_ha_sensor_states(force=True)
        except Exception:
            pass
        try:
            self._sync_virtual_states(force=True)
        except Exception:
            pass
        self._publish_discovery()
        try:
            self._publish_pdc_consensus()
        except Exception:
            pass
        client.publish(f"{self.out_prefix}/status", "online", retain=True)

    # -------------------- Static (profiles/schedule) --------------------

    def _default_season_static(self) -> Dict[str, Any]:
        sea: Dict[str, Any] = {"T1": "NA", "T2": "NA", "T3": "NA", "TM": "NA"}
        for d in DAYS:
            sea[d] = [{"T": "1"} for _ in range(24)]
        return sea

    def _default_therm_static(self) -> Dict[str, Any]:
        return {"WIN": self._default_season_static(), "SUM": self._default_season_static()}

    def _get_therm_static(self, tid: str) -> Dict[str, Any]:
        cur = self.therm_static.get(str(tid))
        out = self._default_therm_static()
        if isinstance(cur, dict):
            for sea in ("WIN", "SUM"):
                sea_cur = cur.get(sea)
                if isinstance(sea_cur, dict):
                    for k in ("T1", "T2", "T3", "TM"):
                        if k in sea_cur:
                            out[sea][k] = sea_cur.get(k)
                    for d in DAYS:
                        arr = sea_cur.get(d)
                        if isinstance(arr, list) and len(arr) == 24:
                            norm = []
                            for it in arr:
                                if isinstance(it, dict):
                                    t = it.get("T") or it.get("t") or ""
                                    norm.append({"T": str(t)})
                                else:
                                    norm.append({"T": str(it)})
                            out[sea][d] = norm
        return out

    def _set_therm_static(self, tid: str, st: Dict[str, Any]) -> None:
        with self.lock:
            self.therm_static[str(tid)] = st
            self.runtime["therm_static"] = self.therm_static
            try:
                save_runtime(self.runtime)
            except Exception:
                pass

    def _merge_static_from_source(self, tid: str, data: Dict[str, Any]) -> None:
        st = self._get_therm_static(tid)
        changed = False
        for sea in ("WIN", "SUM"):
            sea_obj = data.get(sea)
            if not isinstance(sea_obj, dict):
                sea_obj = {}
            for k in ("T1", "T2", "T3", "TM"):
                v = None
                if isinstance(sea_obj, dict) and k in sea_obj:
                    v = sea_obj.get(k)
                if v is not None:
                    st[sea][k] = v
                    changed = True
            for d in DAYS:
                arr = None
                if isinstance(sea_obj, dict) and isinstance(sea_obj.get(d), list):
                    arr = sea_obj.get(d)
                if isinstance(arr, list) and len(arr) == 24:
                    norm = []
                    for it in arr:
                        if isinstance(it, dict):
                            t = it.get("T") or it.get("t") or ""
                            norm.append({"T": str(t)})
                        else:
                            norm.append({"T": str(it)})
                    st[sea][d] = norm
                    changed = True
        if changed:
            self._set_therm_static(tid, st)

    # -------------------- Outputs (power/fan) --------------------

    def _desired_defaults(self) -> Dict[str, Any]:
        return {"power": 0, "fan": {"min": "OFF", "med": "OFF", "max": "OFF"}}

    def _get_desired(self, tid: str) -> Dict[str, Any]:
        cur = self.desired.get(str(tid))
        if not isinstance(cur, dict):
            cur = {}
        out = self._desired_defaults()
        try:
            if cur.get("power") is not None:
                out["power"] = int(float(cur.get("power")))
        except Exception:
            pass
        fan = cur.get("fan")
        if isinstance(fan, dict):
            for k in ("min", "med", "max"):
                if k in fan:
                    out["fan"][k] = str(fan.get(k)).upper()
        return out

    def _set_desired(self, tid: str, desired: Dict[str, Any]) -> None:
        with self.lock:
            self.desired[str(tid)] = desired
            self.runtime["desired"] = self.desired
            try:
                save_runtime(self.runtime)
            except Exception:
                pass

    def _get_desired_season(self, tid: str, season_key: str) -> Dict[str, Any]:
        base = self.desired.get(str(tid))
        if not isinstance(base, dict):
            base = {}
        bucket = base.get(str(season_key))
        if not isinstance(bucket, dict):
            bucket = {}
        out = self._desired_defaults()
        try:
            if bucket.get("power") is not None:
                out["power"] = int(float(bucket.get("power")))
        except Exception:
            pass
        fan = bucket.get("fan")
        if isinstance(fan, dict):
            for k in ("min", "med", "max"):
                if k in fan:
                    out["fan"][k] = str(fan.get(k)).upper()
        return out

    def _set_desired_season(self, tid: str, season_key: str, desired: Dict[str, Any]) -> None:
        with self.lock:
            base = self.desired.get(str(tid))
            if not isinstance(base, dict):
                base = {}
            base[str(season_key)] = desired
            self.desired[str(tid)] = base
            self.runtime["desired"] = self.desired
            try:
                save_runtime(self.runtime)
            except Exception:
                pass

    def _real_entities_for_outputs(self, outputs: Dict[str, Any], targets: Dict[str, Any]) -> set:
        ents = set()
        if not isinstance(outputs, dict) or not isinstance(targets, dict):
            return ents
        if outputs.get("power"):
            pwm_light = (
                targets.get("power_light")
                or targets.get("pwm_light")
                or targets.get("dimmer_light")
                or ""
            )
            for ent in self._split_entities(pwm_light):
                ents.add(str(ent).strip().lower())
            power_switch = (
                targets.get("power_switch")
                or targets.get("relay_switch")
                or targets.get("switch")
                or ""
            )
            for ent in self._split_entities(power_switch):
                ents.add(str(ent).strip().lower())
        if outputs.get("fan3"):
            fan_sw = targets.get("fan_switches") if isinstance(targets.get("fan_switches"), dict) else {}
            for sp in ("min", "med", "max"):
                ent = fan_sw.get(sp) or targets.get(f"fan_{sp}_switch") or ""
                for e in self._split_entities(ent):
                    ents.add(str(e).strip().lower())
        return ents

    def _publish_outputs_state(self, t: Dict[str, Any], season_key: Optional[str] = None, apply_real: bool = True) -> None:
        tid = str(t.get("id"))
        split = self._is_split_outputs(t)
        if not split:
            outputs = t.get("outputs") or {}
            desired = self._get_desired(tid)
            if outputs.get("power"):
                self.mqtt.publish(
                    f"{self.out_prefix}/thermostats/{tid}/power",
                    str(int(desired.get("power", 0))),
                    retain=True,
                )
            if outputs.get("fan3"):
                fan = desired.get("fan") or {}
                for sp in ("min", "med", "max"):
                    val = str(fan.get(sp, "OFF")).upper()
                    val = "ON" if val in ("ON", "1", "TRUE") else "OFF"
                    self.mqtt.publish(
                        f"{self.out_prefix}/thermostats/{tid}/fan/{sp}",
                        val,
                        retain=True,
                    )
            if outputs.get("power") or outputs.get("fan3"):
                power = int(desired.get("power", 0) or 0)
                fan = desired.get("fan") or {}
                fan_on = str(fan.get("min", "OFF")).upper() == "ON" or str(fan.get("med", "OFF")).upper() == "ON" or str(fan.get("max", "OFF")).upper() == "ON"
                valv = "ON" if (power > 0 or fan_on) else "OFF"
                name = _topic_safe_name(t.get("name") or f"vTherm_{tid}")
                self.mqtt.publish(f"{self.out_prefix}/thermostats/{name}/valv/set", valv, retain=True)
                self.mqtt.publish(f"{self.out_prefix}/valv/{tid}/set", valv, retain=True)
            if apply_real:
                self._apply_real_outputs(t, desired, outputs, None)
            return

        sk = season_key or "heat"
        outputs = self._outputs_for_season(t, sk)
        desired = self._get_desired_season(tid, sk)
        base = f"{self.out_prefix}/thermostats/{tid}/{sk}"
        if outputs.get("power"):
            self.mqtt.publish(f"{base}/power", str(int(desired.get("power", 0))), retain=True)
        if outputs.get("fan3"):
            fan = desired.get("fan") or {}
            for sp in ("min", "med", "max"):
                val = str(fan.get(sp, "OFF")).upper()
                val = "ON" if val in ("ON", "1", "TRUE") else "OFF"
                self.mqtt.publish(f"{base}/fan/{sp}", val, retain=True)
        if apply_real:
            self._apply_real_outputs(t, desired, outputs, sk)
        self._publish_valve_state(t)

    def _valve_on_for_therm(self, t: Dict[str, Any]) -> bool:
        tid = str(t.get("id"))
        split = self._is_split_outputs(t)
        if split:
            # fall back to realtime OUT_STATUS if available (HA climate)
            try:
                rt = self.rt.get(tid) or {}
                th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
                out_status = str(th.get("OUT_STATUS") or "").upper()
                if out_status and out_status != "OFF":
                    return True
            except Exception:
                pass
            for sk in ("heat", "cool"):
                outputs = self._outputs_for_season(t, sk)
                if not (outputs.get("power") or outputs.get("fan3")):
                    continue
                desired = self._get_desired_season(tid, sk)
                power = int(desired.get("power", 0) or 0)
                fan = desired.get("fan") or {}
                fan_on = str(fan.get("min", "OFF")).upper() == "ON" or str(fan.get("med", "OFF")).upper() == "ON" or str(fan.get("max", "OFF")).upper() == "ON"
                if power > 0 or fan_on:
                    return True
            return False

        outputs = t.get("outputs") or {}
        if not (outputs.get("power") or outputs.get("fan3")):
            # fall back to realtime OUT_STATUS if available
            try:
                rt = self.rt.get(tid) or {}
                th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
                out_status = str(th.get("OUT_STATUS") or "").upper()
                if out_status and out_status != "OFF":
                    return True
            except Exception:
                pass
            return False
        desired = self._get_desired(tid)
        power = int(desired.get("power", 0) or 0)
        fan = desired.get("fan") or {}
        fan_on = str(fan.get("min", "OFF")).upper() == "ON" or str(fan.get("med", "OFF")).upper() == "ON" or str(fan.get("max", "OFF")).upper() == "ON"
        if power > 0 or fan_on:
            return True
        # fallback to realtime OUT_STATUS if available
        try:
            rt = self.rt.get(tid) or {}
            th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
            out_status = str(th.get("OUT_STATUS") or "").upper()
            if out_status and out_status != "OFF":
                return True
        except Exception:
            pass
        return False

    def _consensus_demand_for_therm(self, t: Dict[str, Any]) -> bool:
        """Return True only when the thermostat is actively requesting demand.

        For PDC consensus we prefer explicit demand/output intent and avoid
        keeping group consensus ON because of stale OUT_STATUS fallback.
        """
        if self._display_only_for(t):
            return False
        tid = str(t.get("id"))
        try:
            rt = self.rt.get(tid) or {}
            th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
            d = str(th.get("DEMAND_ON") or "").upper()
            if d == "ON":
                return True
            if d == "OFF":
                return False
        except Exception:
            pass

        # Fallback when DEMAND_ON is not yet available: use desired outputs only.
        split = self._is_split_outputs(t)
        if split:
            for sk in ("heat", "cool"):
                outputs = self._outputs_for_season(t, sk)
                if not (outputs.get("power") or outputs.get("fan3")):
                    continue
                desired = self._get_desired_season(tid, sk)
                power = int(desired.get("power", 0) or 0)
                fan = desired.get("fan") or {}
                fan_on = (
                    str(fan.get("min", "OFF")).upper() == "ON"
                    or str(fan.get("med", "OFF")).upper() == "ON"
                    or str(fan.get("max", "OFF")).upper() == "ON"
                )
                if power > 0 or fan_on:
                    return True
            return False

        outputs = t.get("outputs") or {}
        if not (outputs.get("power") or outputs.get("fan3")):
            return False
        desired = self._get_desired(tid)
        power = int(desired.get("power", 0) or 0)
        fan = desired.get("fan") or {}
        fan_on = (
            str(fan.get("min", "OFF")).upper() == "ON"
            or str(fan.get("med", "OFF")).upper() == "ON"
            or str(fan.get("max", "OFF")).upper() == "ON"
        )
        return bool(power > 0 or fan_on)

    def _calc_auto_valves(self, t: Dict[str, Any]) -> tuple[bool, bool]:
        """Return (low_on, hot_on) for automatic logic."""
        tid = str(t.get("id"))
        # Use the same demand signal used by consensus groups to avoid
        # stale OUT_STATUS-driven valve activations.
        demand = self._consensus_demand_for_therm(t)
        sea = ""
        try:
            rt = self.rt.get(tid) or {}
            th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
            sea = str(th.get("ACT_SEA") or "").upper()
        except Exception:
            sea = ""
        hot_on = False
        low_on = False
        if demand:
            if sea == "SUM":
                hot_on = True
                low_on = False
            elif sea == "WIN":
                hot_on = True
                low_on = True
            else:
                # Unknown season: fail-safe OFF to avoid energizing floor loop.
                hot_on = False
                low_on = False
        return (low_on, hot_on)

    def _publish_valve_state(self, t: Dict[str, Any]) -> None:
        """Publish valve state ON when any relevant demand is active."""
        tid = str(t.get("id"))
        now = time.time()
        ov_until = float(self._manual_valve_until.get(tid, 0.0) or 0.0)
        if ov_until and now < ov_until:
            st = self._manual_valve_state.get(tid) or {}
            low_on = bool(st.get("low"))
            hot_on = bool(st.get("hot"))
        else:
            if ov_until and now >= ov_until:
                self._manual_valve_until.pop(tid, None)
                self._manual_valve_state.pop(tid, None)
            low_on, hot_on = self._calc_auto_valves(t)

        valv = "ON" if (hot_on or low_on) else "OFF"
        name = _topic_safe_name(t.get("name") or f"vTherm_{tid}")
        self.mqtt.publish(f"{self.out_prefix}/thermostats/{name}/valv/state", valv, retain=True)
        self.mqtt.publish(f"{self.out_prefix}/valv/{tid}/state", valv, retain=True)
        self.mqtt.publish(f"{self.out_prefix}/thermostats/{name}/valv_hot/state", "ON" if hot_on else "OFF", retain=True)
        self.mqtt.publish(f"{self.out_prefix}/valv_hot/{tid}/state", "ON" if hot_on else "OFF", retain=True)
        self.mqtt.publish(f"{self.out_prefix}/thermostats/{name}/valv_low/state", "ON" if low_on else "OFF", retain=True)
        self.mqtt.publish(f"{self.out_prefix}/valv_low/{tid}/state", "ON" if low_on else "OFF", retain=True)
        self._apply_real_valves(t, low_on, hot_on)
        # Keep global PDC consensus in sync with every valve update.
        self._publish_pdc_consensus()

    def _publish_pdc_consensus(self) -> None:
        """Publish general and seasonal PDC consensus topics by source group."""
        on_esafe = False
        on_esafe_heat = False
        on_esafe_cool = False
        on_ha = False
        on_ha_heat = False
        on_ha_cool = False
        try:
            for t in self.therm_list():
                if self._display_only_for(t):
                    continue
                if not self._consensus_demand_for_therm(t):
                    continue
                src = t.get("source") if isinstance(t.get("source"), dict) else {}
                src_type = str((src or {}).get("type") or "").strip().lower()
                is_ha = src_type in ("ha_climate", "homeassistant_climate", "ha")
                tid = str(t.get("id"))
                sea = ""
                try:
                    rt = self.rt.get(tid) or {}
                    th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
                    sea = str(th.get("ACT_SEA") or "").upper()
                except Exception:
                    sea = ""
                if is_ha:
                    on_ha = True
                    if sea == "SUM":
                        on_ha_cool = True
                    else:
                        on_ha_heat = True
                else:
                    on_esafe = True
                    if sea == "SUM":
                        on_esafe_cool = True
                    else:
                        on_esafe_heat = True
        except Exception:
            on_esafe = False
            on_esafe_heat = False
            on_esafe_cool = False
            on_ha = False
            on_ha_heat = False
            on_ha_cool = False

        # Legacy PDC topics: now driven only by legacy/e-safe thermostats.
        self.mqtt.publish(f"{self.out_prefix}/pdc/state", "ON" if on_esafe else "OFF", retain=True)
        self.mqtt.publish(f"{self.out_prefix}/pdc/heat/state", "ON" if on_esafe_heat else "OFF", retain=True)
        self.mqtt.publish(f"{self.out_prefix}/pdc/cool/state", "ON" if on_esafe_cool else "OFF", retain=True)

        # Dedicated PDC topics for HA-climate sourced thermostats.
        self.mqtt.publish(f"{self.out_prefix}/pdc/ha/state", "ON" if on_ha else "OFF", retain=True)
        self.mqtt.publish(f"{self.out_prefix}/pdc/ha/heat/state", "ON" if on_ha_heat else "OFF", retain=True)
        self.mqtt.publish(f"{self.out_prefix}/pdc/ha/cool/state", "ON" if on_ha_cool else "OFF", retain=True)

        # User-mapped consensus groups (per thermostat, persistent config field: consensus_group_heat/cool).
        groups: Dict[str, Dict[str, Any]] = {}
        try:
            all_therms = list(self.therm_list())
            for t in all_therms:
                if self._display_only_for(t):
                    continue
                for g_label in [
                    str(t.get("consensus_group_heat") or t.get("consensus_group") or t.get("pdc_group") or "").strip(),
                    str(t.get("consensus_group_cool") or t.get("consensus_group") or t.get("pdc_group") or "").strip(),
                ]:
                    if not g_label:
                        continue
                    g_key = _topic_safe_name(g_label).lower()
                    if g_key not in groups:
                        groups[g_key] = {"label": g_label, "on": False, "on_heat": False, "on_cool": False}

            # Include configured consensus_groups even if no thermostat currently references them.
            cfg_groups = self.cfg.get("consensus_groups") if isinstance(self.cfg, dict) else []
            if isinstance(cfg_groups, list):
                for g in cfg_groups:
                    if not isinstance(g, dict):
                        continue
                    g_label = str(g.get("name") or "").strip()
                    if not g_label:
                        continue
                    g_key = _topic_safe_name(g_label).lower()
                    if g_key not in groups:
                        groups[g_key] = {"label": g_label, "on": False, "on_heat": False, "on_cool": False}

            for t in all_therms:
                if self._display_only_for(t):
                    continue
                if not self._consensus_demand_for_therm(t):
                    continue
                g_heat = str(t.get("consensus_group_heat") or t.get("consensus_group") or t.get("pdc_group") or "").strip()
                g_cool = str(t.get("consensus_group_cool") or t.get("consensus_group") or t.get("pdc_group") or "").strip()
                tid = str(t.get("id"))
                sea = ""
                try:
                    rt = self.rt.get(tid) or {}
                    th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
                    sea = str(th.get("ACT_SEA") or "").upper()
                except Exception:
                    sea = ""
                if sea == "SUM":
                    if g_cool:
                        gk = _topic_safe_name(g_cool).lower()
                        if gk in groups:
                            groups[gk]["on"] = True
                            groups[gk]["on_cool"] = True
                else:
                    # In HEAT, enable both: heat group + cool group
                    if g_heat:
                        gk = _topic_safe_name(g_heat).lower()
                        if gk in groups:
                            groups[gk]["on"] = True
                            groups[gk]["on_heat"] = True
                    if g_cool:
                        gk = _topic_safe_name(g_cool).lower()
                        if gk in groups:
                            groups[gk]["on"] = True
                            groups[gk]["on_heat"] = True
        except Exception:
            groups = {}

        for g_key, st in groups.items():
            self.mqtt.publish(f"{self.out_prefix}/pdc/groups/{g_key}/state", "ON" if st.get("on") else "OFF", retain=True)
            self.mqtt.publish(
                f"{self.out_prefix}/pdc/groups/{g_key}/heat/state",
                "ON" if st.get("on_heat") else "OFF",
                retain=True,
            )
            self.mqtt.publish(
                f"{self.out_prefix}/pdc/groups/{g_key}/cool/state",
                "ON" if st.get("on_cool") else "OFF",
                retain=True,
            )

        # Drive real HA switches for consensus groups (if configured).
        # A physical switch may appear in multiple groups; apply it once with OR logic.
        try:
            reserved_switches = self._reserved_real_switch_entities()
            cfg_groups = self.cfg.get("consensus_groups") if isinstance(self.cfg, dict) else []
            if not isinstance(cfg_groups, list):
                cfg_groups = []
            desired_switches: Dict[str, bool] = {}
            desired_switch_names: Dict[str, str] = {}

            def queue_switch(entity_id: str, desired_on: bool) -> None:
                ent = str(entity_id or "").strip()
                if not ent:
                    return
                ek = ent.lower()
                if ek in reserved_switches:
                    if ek not in self._real_switch_skip_warned:
                        self._real_switch_skip_warned.add(ek)
                        print(f"[WARN] consensus skip reserved thermostat switch: {ek}")
                    return
                desired_switch_names.setdefault(ek, ent)
                desired_switches[ek] = bool(desired_switches.get(ek, False) or bool(desired_on))

            for g in cfg_groups:
                if not isinstance(g, dict):
                    continue
                name = str(g.get("name") or "").strip()
                if not name:
                    continue
                g_key = _topic_safe_name(name).lower()
                st = groups.get(g_key) or {}
                sw = str(g.get("switch") or g.get("general_switch") or g.get("consensus_switch") or "").strip()
                sw_h = str(g.get("switch_heat") or g.get("heat_switch") or "").strip()
                sw_c = str(g.get("switch_cool") or g.get("cool_switch") or "").strip()
                if sw:
                    for e in self._split_entities(sw):
                        queue_switch(e, bool(st.get("on")))
                if sw_h:
                    for e in self._split_entities(sw_h):
                        queue_switch(e, bool(st.get("on_heat")))
                if sw_c:
                    for e in self._split_entities(sw_c):
                        queue_switch(e, bool(st.get("on_cool")))
            for ek, desired_on in desired_switches.items():
                self._apply_real_switch(desired_switch_names.get(ek, ek), bool(desired_on))
        except Exception:
            pass

    # -------------------- HA clone (MQTT climate) --------------------

    def _ha_base(self, tid: str) -> str:
        return f"{self.out_prefix}/thermostats/{tid}"

    def _ha_publish_clone_state(self, tid: str) -> None:
        t = self._find_by_id(str(tid)) or {}
        with self.lock:
            rt = self.rt.get(str(tid)) or {}
            th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}

        temp = rt.get("TEMP")
        rh = rt.get("RH")
        preset = th.get("ACT_MODEL") or th.get("ACT_MODE") or ""
        sea = str(th.get("ACT_SEA") or "").upper()

        # hvac_mode mapping
        hvac_mode = "off"
        if str(preset).upper() == "OFF":
            hvac_mode = "off"
        else:
            if sea == "WIN":
                hvac_mode = "heat"
            elif sea == "SUM":
                hvac_mode = "cool"
        if hvac_mode not in self._therm_allowed_modes(t):
            hvac_mode = "off"

        # target
        tgt = None
        thr = th.get("TEMP_THR") if isinstance(th.get("TEMP_THR"), dict) else None
        if thr and thr.get("VAL") is not None:
            tgt = _as_float(thr.get("VAL"))

        base = self._ha_base(str(tid))
        if temp is not None:
            self.mqtt.publish(f"{base}/current_temperature", str(float(temp)), retain=True)
        if rh is not None:
            self.mqtt.publish(f"{base}/humidity", str(float(rh)), retain=True)
        if tgt is not None:
            self.mqtt.publish(f"{base}/target_temperature", str(float(tgt)), retain=True)

        self.mqtt.publish(f"{base}/mode", hvac_mode, retain=True)
        if preset:
            self.mqtt.publish(f"{base}/preset_mode", str(preset).upper(), retain=True)

    def _handle_ha_clone_command(self, tid: str, kind: str, payload_raw: str, origin: str = "ha_mqtt") -> None:
        t = self._find_by_id(tid)
        if not t:
            return
        if self._display_only_for(t):
            return
        src = t.get("source") or {}
        stype = str(src.get("type", "")).lower()
        is_esafe = stype in ("esafe", "esafe_json")
        is_ha = stype in ("ha_climate", "homeassistant_climate", "ha")
        is_ha_avg = stype in ("ha_multi_sensor_avg", "ha_sensor_avg", "ha_multi_avg")
        is_ha_sensor = stype in ("ha_sensor", "homeassistant_sensor", "sensor")
        is_virtual = stype in ("virtual", "local", "local_virtual")
        if not (is_esafe or is_ha or is_ha_avg or is_ha_sensor or is_virtual):
            return
        num = None
        if is_esafe:
            try:
                num = int(src.get("num"))
            except Exception:
                return
        real_ent = self._real_thermostat_entity(t)
        helper_ent = self._ha_helper_climate_entity(t)
        ent = str(src.get("entity_id") or "").strip() if is_ha else (real_ent or helper_ent)
        if (is_ha or is_ha_avg) and not ent:
            return
        rtcfg = self._real_thermostat_cfg(t)
        can_bridge_real_climate = bool(ent) and (is_ha or is_ha_avg or is_ha_sensor or is_virtual)
        adaptive_cfg = rtcfg.get("adaptive_demand_setpoint")
        adaptive_enabled = True if (is_ha_avg and bool(real_ent)) else (bool(adaptive_cfg) if adaptive_cfg is not None else False)
        sync_setp = self._bool_cfg(rtcfg, "sync_setpoint", True)
        sync_mode = self._bool_cfg(rtcfg, "sync_hvac_mode", True)
        sync_preset = self._bool_cfg(rtcfg, "sync_preset_mode", True)
        # In adaptive mode the real thermostat setpoint is driven by demand logic
        # (real ambient +/- delta). Avoid overwriting it with virtual target commands.
        if adaptive_enabled and is_ha_avg and real_ent:
            sync_setp = False
        name = str(t.get("name") or f"vTherm {tid}")

        def publish_clone_now() -> None:
            try:
                self._ha_publish_clone_state(str(tid))
            except Exception:
                pass

        # ensure rt/therm exist
        with self.lock:
            rt = self.rt.setdefault(str(tid), {})
            th = rt.setdefault("THERM", {})

        if kind == "target_temperature":
            v = _as_float(payload_raw)
            if v is None:
                return
            v = self._clamp_therm_target(t, float(v))
            with self.lock:
                rt0 = self.rt.setdefault(str(tid), {})
                th0 = rt0.setdefault("THERM", {})
                old_v = None
                thr0 = th0.get("TEMP_THR") if isinstance(th0.get("TEMP_THR"), dict) else None
                if thr0 and thr0.get("VAL") is not None:
                    old_v = _as_float(thr0.get("VAL"))
            if is_esafe:
                self.mqtt.publish(f"{self.source_prefix}/cmd/thermostat/{num}/temperature", str(v), retain=False)
            elif can_bridge_real_climate:
                if sync_setp:
                    preferred_mode = ""
                    try:
                        with self.lock:
                            rt_pref = self.rt.get(str(tid)) or {}
                            th_pref = rt_pref.get("THERM") if isinstance(rt_pref.get("THERM"), dict) else {}
                            model_pref = str(th_pref.get("ACT_MODEL") or th_pref.get("ACT_MODE") or "").upper()
                            sea_pref = str(th_pref.get("ACT_SEA") or "").upper()
                        if model_pref != "OFF":
                            if sea_pref == "WIN":
                                preferred_mode = "heat"
                            elif sea_pref == "SUM":
                                preferred_mode = "cool"
                    except Exception:
                        preferred_mode = ""
                    self._ha_climate_set_temperature_safe(ent, float(v), preferred_mode)
                    self._ha_bridge_setpoint_hold[str(tid)] = time.time() + 8.0
            try:
                self._register_ack(tid=str(tid), field="setpoint", origin=origin, expected=float(v))
            except Exception:
                pass
            if self._log_enabled("MIN"):
                try:
                    self._log_event(
                        origin=origin,
                        tid=str(tid),
                        name=name,
                        source_num=num,
                        category="cmd",
                        field="setpoint",
                        old=old_v,
                        new=float(v),
                        msg="setpoint command sent",
                    )
                except Exception:
                    pass
            with self.lock:
                rt = self.rt.setdefault(str(tid), {})
                th = rt.setdefault("THERM", {})
                th["TEMP_THR"] = {"VAL": float(v)}
            publish_clone_now()
            self._sync_ui()
            self._persist_rt_cache()
            return

        if kind == "mode":
            m = str(payload_raw or "").strip().lower()
            if m not in ("heat", "cool", "off"):
                return
            allowed_modes = self._therm_allowed_modes(t)
            if m not in allowed_modes:
                # Cool-only or heat-only thermostats should never enter an unsupported HVAC mode.
                m = "off"
            if m == "off" and origin == "ha_mqtt":
                hold = self._ha_bridge_mode_hold.get(str(tid))
                if isinstance(hold, dict):
                    hold_until = float(hold.get("until", 0.0) or 0.0)
                    hold_mode = str(hold.get("mode") or "").strip().lower()
                    if time.time() <= hold_until and hold_mode in ("heat", "cool"):
                        try:
                            self._log_event(
                                origin=origin,
                                tid=str(tid),
                                name=name,
                                source_num=num,
                                category="cmd",
                                field="season",
                                old=hold_mode,
                                new="off",
                                msg="ignored transient OFF command during HA climate mode hold",
                            )
                        except Exception:
                            pass
                        return
            with self.lock:
                rt0 = self.rt.setdefault(str(tid), {})
                th0 = rt0.setdefault("THERM", {})
                old_sea = str(th0.get("ACT_SEA") or "").upper() or None
            if is_esafe:
                self.mqtt.publish(f"{self.source_prefix}/cmd/thermostat/{num}/mode", m, retain=False)
            elif can_bridge_real_climate:
                if sync_mode:
                    self._ha_climate_set_hvac_mode_safe(ent, m)
                    if m in ("heat", "cool") and sync_setp:
                        try:
                            with self.lock:
                                rt_cmd = self.rt.get(str(tid)) or {}
                                th_cmd = rt_cmd.get("THERM") if isinstance(rt_cmd.get("THERM"), dict) else {}
                                thr_cmd = th_cmd.get("TEMP_THR") if isinstance(th_cmd.get("TEMP_THR"), dict) else None
                                target_cmd = _as_float(thr_cmd.get("VAL")) if isinstance(thr_cmd, dict) else None
                            if target_cmd is not None:
                                self._ha_climate_set_temperature_safe(ent, float(target_cmd), m)
                                self._ha_bridge_setpoint_hold[str(tid)] = time.time() + 8.0
                        except Exception:
                            pass
                    if m in ("heat", "cool"):
                        self._ha_bridge_mode_hold[str(tid)] = {"mode": m, "until": time.time() + 30.0}
                    else:
                        self._ha_bridge_mode_hold.pop(str(tid), None)
            new_sea = "WIN" if m == "heat" else ("SUM" if m == "cool" else "OFF")
            try:
                self._register_ack(tid=str(tid), field="season", origin=origin, expected=new_sea)
            except Exception:
                pass
            if self._log_enabled("MIN"):
                try:
                    self._log_event(
                        origin=origin,
                        tid=str(tid),
                        name=name,
                        source_num=num,
                        category="cmd",
                        field="season",
                        old=old_sea,
                        new=new_sea,
                        msg="season/mode command sent",
                    )
                except Exception:
                    pass
            with self.lock:
                rt = self.rt.setdefault(str(tid), {})
                th = rt.setdefault("THERM", {})
                if m == "heat":
                    th["ACT_SEA"] = "WIN"
                    th["ACT_MODEL"] = "MAN"
                elif m == "cool":
                    th["ACT_SEA"] = "SUM"
                    th["ACT_MODEL"] = "MAN"
                else:
                    th["ACT_SEA"] = "OFF"
                    th["ACT_MODEL"] = "OFF"
            publish_clone_now()
            self._sync_ui()
            self._persist_rt_cache()
            return

        if kind == "preset_mode":
            p = str(payload_raw or "").strip().upper()
            if not p:
                return
            # OFF must always map to a real HVAC OFF command.
            if p == "OFF":
                if origin == "ha_mqtt":
                    hold = self._ha_bridge_mode_hold.get(str(tid))
                    if isinstance(hold, dict):
                        hold_until = float(hold.get("until", 0.0) or 0.0)
                        hold_mode = str(hold.get("mode") or "").strip().lower()
                        if time.time() <= hold_until and hold_mode in ("heat", "cool"):
                            try:
                                self._log_event(
                                    origin=origin,
                                    tid=str(tid),
                                    name=name,
                                    source_num=num,
                                    category="cmd",
                                    field="preset",
                                    old=hold_mode,
                                    new="OFF",
                                    msg="ignored transient preset OFF during HA climate mode hold",
                                )
                            except Exception:
                                pass
                            return
                self._handle_ha_clone_command(tid, "mode", "off", origin=origin)
                return
            with self.lock:
                rt0 = self.rt.setdefault(str(tid), {})
                th0 = rt0.setdefault("THERM", {})
                old_p = str(th0.get("ACT_MODEL") or th0.get("ACT_MODE") or "").upper() or None
            if is_esafe:
                self.mqtt.publish(f"{self.source_prefix}/cmd/thermostat/{num}/preset_mode", p, retain=False)
            elif can_bridge_real_climate:
                if sync_preset:
                    self._ha_climate_service(ent, "set_preset_mode", {"preset_mode": p.lower()})
            try:
                self._register_ack(tid=str(tid), field="mode", origin=origin, expected=p)
            except Exception:
                pass
            if self._log_enabled("MIN"):
                try:
                    self._log_event(
                        origin=origin,
                        tid=str(tid),
                        name=name,
                        source_num=num,
                        category="cmd",
                        field="mode",
                        old=old_p,
                        new=p,
                        msg="preset/mode command sent",
                    )
                except Exception:
                    pass
            with self.lock:
                rt = self.rt.setdefault(str(tid), {})
                th = rt.setdefault("THERM", {})
                th["ACT_MODEL"] = p
            publish_clone_now()
            self._sync_ui()
            self._persist_rt_cache()
            return

    # -------------------- Combined out_prefix handler --------------------

    def _handle_out_prefix_command(self, topic: str, payload_raw: str) -> None:
        base = f"{self.out_prefix}/thermostats/"
        if not topic.startswith(base):
            return
        rest = topic[len(base) :]
        parts = [p for p in rest.split("/") if p]
        if len(parts) < 3:
            return
        tid = parts[0]
        t = self._find_by_id(tid)
        if not t:
            return
        if self._display_only_for(t):
            return
        split = self._is_split_outputs(t)

        def _set_override(sk: Optional[str]) -> None:
            key = f"{tid}:{sk}" if (split and sk) else tid
            self._manual_override_until[str(key)] = time.time() + float(self._override_sec_for(t))

        # Seasonal outputs (when configured): /<tid>/<heat|cool>/power/set and /<tid>/<heat|cool>/fan/<sp>/set
        if split and len(parts) >= 4 and parts[1] in ("heat", "cool"):
            sk = parts[1]
            _set_override(sk)
            if parts[2] == "power" and parts[3] == "set":
                desired = self._get_desired_season(tid, sk)
                old_v = desired.get("power")
                v = _as_float(payload_raw)
                if v is None:
                    return
                desired["power"] = int(round(max(0.0, min(100.0, v))))
                self._set_desired_season(tid, sk, desired)
                self._publish_outputs_state(t, sk)
                if self._log_enabled("MIN"):
                    try:
                        self._log_event(
                            origin="ha_mqtt",
                            tid=str(tid),
                            name=str(t.get("name") or f"vTherm {tid}"),
                            source_num=int((t.get("source") or {}).get("num")) if (t.get("source") or {}).get("num") is not None else None,
                            category="out",
                            field=f"{sk}.power",
                            old=old_v,
                            new=desired.get("power"),
                            msg="manual output set",
                        )
                    except Exception:
                        pass
                return
            if parts[2] == "fan" and len(parts) >= 6 and parts[5] == "set":
                sp = parts[3].lower()
                if sp not in ("min", "med", "max"):
                    return
                desired = self._get_desired_season(tid, sk)
                old_f = (desired.get("fan") or {}).copy() if isinstance(desired.get("fan"), dict) else desired.get("fan")
                on = str(payload_raw or "").strip().upper() in ("ON", "1", "TRUE", "YES")
                fan = desired.get("fan") or {"min": "OFF", "med": "OFF", "max": "OFF"}
                if on:
                    for k in ("min", "med", "max"):
                        fan[k] = "ON" if k == sp else "OFF"
                else:
                    fan[sp] = "OFF"
                desired["fan"] = fan
                self._set_desired_season(tid, sk, desired)
                self._publish_outputs_state(t, sk)
                if self._log_enabled("MIN"):
                    try:
                        self._log_event(
                            origin="ha_mqtt",
                            tid=str(tid),
                            name=str(t.get("name") or f"vTherm {tid}"),
                            source_num=int((t.get("source") or {}).get("num")) if (t.get("source") or {}).get("num") is not None else None,
                            category="out",
                            field=f"{sk}.fan",
                            old=old_f,
                            new=desired.get("fan"),
                            msg=f"manual fan set ({sp}={'ON' if on else 'OFF'})",
                        )
                    except Exception:
                        pass
                return

        # power
        if parts[1] == "power" and parts[2] == "set":
            _set_override(None)
            desired = self._get_desired(tid)
            old_v = desired.get("power")
            v = _as_float(payload_raw)
            if v is None:
                return
            desired["power"] = int(round(max(0.0, min(100.0, v))))
            self._set_desired(tid, desired)
            self._publish_outputs_state(t)
            if self._log_enabled("MIN"):
                try:
                    self._log_event(
                        origin="ha_mqtt",
                        tid=str(tid),
                        name=str(t.get("name") or f"vTherm {tid}"),
                        source_num=int((t.get("source") or {}).get("num")) if (t.get("source") or {}).get("num") is not None else None,
                        category="out",
                        field="power",
                        old=old_v,
                        new=desired.get("power"),
                        msg="manual output set",
                    )
                except Exception:
                    pass
            return

        # fan
        if parts[1] == "fan" and len(parts) >= 4 and parts[3] == "set":
            _set_override(None)
            sp = parts[2].lower()
            if sp not in ("min", "med", "max"):
                return
            desired = self._get_desired(tid)
            old_f = (desired.get("fan") or {}).copy() if isinstance(desired.get("fan"), dict) else desired.get("fan")
            on = str(payload_raw or "").strip().upper() in ("ON", "1", "TRUE", "YES")
            fan = desired.get("fan") or {"min": "OFF", "med": "OFF", "max": "OFF"}
            if on:
                for k in ("min", "med", "max"):
                    fan[k] = "ON" if k == sp else "OFF"
            else:
                fan[sp] = "OFF"
            desired["fan"] = fan
            self._set_desired(tid, desired)
            self._publish_outputs_state(t)
            if self._log_enabled("MIN"):
                try:
                    self._log_event(
                        origin="ha_mqtt",
                        tid=str(tid),
                        name=str(t.get("name") or f"vTherm {tid}"),
                        source_num=int((t.get("source") or {}).get("num")) if (t.get("source") or {}).get("num") is not None else None,
                        category="out",
                        field="fan",
                        old=old_f,
                        new=desired.get("fan"),
                        msg=f"manual fan set ({sp}={'ON' if on else 'OFF'})",
                    )
                except Exception:
                    pass
            return

        # HA clone
        if parts[1] in ("target_temperature", "mode", "preset_mode") and parts[2] == "set":
            self._handle_ha_clone_command(tid, parts[1], payload_raw, origin="ha_mqtt")
            return

    def _handle_valv_command(self, topic: str, payload_raw: str) -> bool:
        base = f"{self.out_prefix}/"
        if not topic.startswith(base):
            return False
        rest = topic[len(base) :]
        parts = [p for p in rest.split("/") if p]
        if len(parts) != 3 or parts[2] != "set":
            return False
        kind = parts[0]
        if kind not in ("valv", "valv_hot", "valv_low"):
            return False
        tid = parts[1]
        t = self._find_by_id(tid)
        if not t:
            return True
        if self._display_only_for(t):
            return True
        on = str(payload_raw or "").strip().upper() in ("ON", "1", "TRUE", "YES")
        low_on, hot_on = self._calc_auto_valves(t)
        sea = ""
        try:
            rt = self.rt.get(str(tid)) or {}
            th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
            sea = str(th.get("ACT_SEA") or "").upper()
        except Exception:
            sea = ""
        if kind == "valv":
            # Generic valve command must honor season:
            # SUM => only high-temp loop, WIN => both loops.
            if sea == "SUM":
                low_on = False
                hot_on = bool(on)
            elif sea == "WIN":
                low_on = bool(on)
                hot_on = bool(on)
            else:
                low_on = False
                hot_on = bool(on)
        elif kind == "valv_hot":
            hot_on = on
        elif kind == "valv_low":
            low_on = on

        self._manual_valve_state[str(tid)] = {"low": bool(low_on), "hot": bool(hot_on)}
        self._manual_valve_until[str(tid)] = time.time() + float(self._override_sec_for(t))
        self._publish_valve_state(t)
        return True

    def _clear_retained_command_topics(self) -> None:
        try:
            ids: List[int] = []
            for t in self.therm_list():
                try:
                    ids.append(int(t.get("id")))
                except Exception:
                    continue
            if not ids:
                return
            max_id = max(ids)
            for tid in range(1, max_id + 1):
                self.mqtt.publish(f"{self.out_prefix}/thermostats/{tid}/target_temperature/set", "", retain=True)
                self.mqtt.publish(f"{self.out_prefix}/thermostats/{tid}/mode/set", "", retain=True)
                self.mqtt.publish(f"{self.out_prefix}/thermostats/{tid}/preset_mode/set", "", retain=True)
                self.mqtt.publish(f"{self.out_prefix}/valv/{tid}/set", "", retain=True)
                self.mqtt.publish(f"{self.out_prefix}/valv_hot/{tid}/set", "", retain=True)
                self.mqtt.publish(f"{self.out_prefix}/valv_low/{tid}/set", "", retain=True)
        except Exception:
            pass

    # -------------------- Source handler --------------------

    def _on_message(self, client, userdata, msg):
        try:
            self._last_mqtt_any_ts = time.time()
        except Exception:
            pass
        topic = msg.topic
        payload_raw = msg.payload.decode("utf-8", errors="ignore").strip()

        if topic.startswith("homeassistant/climate/") and topic.endswith("/config"):
            if self._handle_discovery_config_message(topic, payload_raw):
                return

        if topic.startswith(f"{self.out_prefix}/thermostats/"):
            # Stability: ignore retained "command" messages (*/set) that might be left on the broker.
            # Otherwise on (re)subscribe we would apply an old command and trigger manual override,
            # which looks like "auto control blocked" for auto_override_sec seconds.
            try:
                if bool(getattr(msg, "retain", False)) and topic.endswith("/set"):
                    if payload_raw == "":
                        return
                    print(f"[WARN] Ignoring retained command on {topic}")
                    return
            except Exception:
                pass
            self._handle_out_prefix_command(topic, payload_raw)
            return

        if topic.startswith(f"{self.out_prefix}/valv") or topic.startswith(f"{self.out_prefix}/valv_hot") or topic.startswith(f"{self.out_prefix}/valv_low"):
            try:
                if bool(getattr(msg, "retain", False)) and topic.endswith("/set"):
                    if payload_raw == "":
                        return
                    print(f"[WARN] Ignoring retained command on {topic}")
                    return
            except Exception:
                pass
            if self._handle_valv_command(topic, payload_raw):
                return

        if not topic.startswith(f"{self.source_prefix}/thermostats/"):
            return

        try:
            rest = topic.split(f"{self.source_prefix}/thermostats/")[1]
        except Exception:
            return

        if "/" in rest:
            num_s, key = rest.split("/", 1)
        else:
            num_s, key = rest, None

        try:
            num = int(num_s)
        except Exception:
            return

        t = self._find_by_source_num(num)
        if not t:
            return
        tid = str(t.get("id"))
        try:
            self._last_source_ts = time.time()
            self._ever_got_source = True
        except Exception:
            pass

        if key is None:
            try:
                data = json.loads(payload_raw or "{}")
            except Exception:
                return
            if not isinstance(data, dict):
                return

            cur = _get_any(data, "TEMP")
            rh = _get_any(data, "RH")
            therm = data.get("THERM") if isinstance(data.get("THERM"), dict) else {}

            season = _get_any(therm, "ACT_SEA") or _get_any(data, "ACT_SEA")
            model = _get_any(therm, "ACT_MODEL") or _get_any(data, "ACT_MODE")
            out_status = _get_any(therm, "OUT_STATUS") or _get_any(data, "OUT_STATUS")

            temp_thr = _get_any(therm, ("TEMP_THR", "VAL"))
            target = None
            if temp_thr is not None:
                target = temp_thr
            diffs: List[Dict[str, Any]] = []
            with self.lock:
                rt = self.rt.setdefault(tid, {})
                old_temp = rt.get("TEMP")
                old_rh = rt.get("RH")
                th = rt.setdefault("THERM", {})
                old_sea = th.get("ACT_SEA")
                old_model = th.get("ACT_MODEL") or th.get("ACT_MODE")
                old_out_status = th.get("OUT_STATUS")
                old_thr = None
                thr0 = th.get("TEMP_THR") if isinstance(th.get("TEMP_THR"), dict) else None
                if thr0 and thr0.get("VAL") is not None:
                    old_thr = _as_float(thr0.get("VAL"))

                if cur is not None:
                    v = _as_float(cur)
                    if v is not None:
                        rt["TEMP"] = v
                if rh is not None:
                    v = _as_float(rh)
                    if v is not None:
                        rt["RH"] = v

                if season:
                    sea_v = str(season).upper()
                    allowed_modes = self._therm_allowed_modes(t)
                    if sea_v == "WIN" and "heat" not in allowed_modes and "cool" in allowed_modes:
                        sea_v = "SUM"
                    if sea_v == "SUM" and "cool" not in allowed_modes:
                        sea_v = "OFF"
                    th["ACT_SEA"] = sea_v
                if model:
                    th["ACT_MODEL"] = str(model).upper()
                if out_status:
                    th["OUT_STATUS"] = str(out_status).upper()
                if target is not None:
                    v = _as_float(target)
                    if v is not None:
                        # Anti-rollback: if a setpoint command is pending ACK, do not
                        # overwrite local TEMP_THR with stale source value.
                        keep_local = False
                        try:
                            ack = self._pending_acks.get(self._ack_key(str(tid), "setpoint")) or {}
                            if ack:
                                exp = _as_float(ack.get("expected"))
                                ts0 = float(ack.get("ts") or 0.0)
                                if exp is not None and ts0 and (time.time() - ts0) <= float(self.log_ack_timeout_sec):
                                    if abs(float(v) - float(exp)) > 0.05:
                                        keep_local = True
                        except Exception:
                            keep_local = False
                        if not keep_local:
                            th["TEMP_THR"] = {"VAL": float(v)}

                # Best-effort ACT_MODE mapping
                sea_up = str(th.get("ACT_SEA") or "").upper()
                out_up = str(th.get("OUT_STATUS") or "").upper()
                if sea_up == "WIN":
                    th["ACT_MODE"] = "HEA" if out_up != "OFF" else "OFF"
                elif sea_up == "SUM":
                    th["ACT_MODE"] = "COO" if out_up != "OFF" else "OFF"

                new_temp = rt.get("TEMP")
                new_rh = rt.get("RH")
                new_sea = th.get("ACT_SEA")
                new_model = th.get("ACT_MODEL") or th.get("ACT_MODE")
                new_out_status = th.get("OUT_STATUS")
                new_thr = None
                thr1 = th.get("TEMP_THR") if isinstance(th.get("TEMP_THR"), dict) else None
                if thr1 and thr1.get("VAL") is not None:
                    new_thr = _as_float(thr1.get("VAL"))

                if old_sea != new_sea and new_sea is not None:
                    diffs.append({"cat": "state", "field": "season", "old": old_sea, "new": new_sea})
                if old_model != new_model and new_model is not None:
                    diffs.append({"cat": "state", "field": "mode", "old": old_model, "new": new_model})
                if old_out_status != new_out_status and new_out_status is not None:
                    diffs.append({"cat": "state", "field": "out_status", "old": old_out_status, "new": new_out_status})
                if old_thr != new_thr and new_thr is not None:
                    diffs.append({"cat": "state", "field": "setpoint", "old": old_thr, "new": new_thr})

                # Rate-limited logging for telemetry (only in DEBUG by default)
                try:
                    now2 = time.time()
                    rec = self._last_temp_log.get(str(tid)) or {}
                    last_temp = _as_float(rec.get("temp"))
                    last_rh = _as_float(rec.get("rh"))
                    last_ts = float(rec.get("ts") or 0.0)
                    if new_temp is not None:
                        dt = now2 - last_ts if last_ts else 1e9
                        if self._log_enabled("DEBUG") and (
                            (last_temp is None)
                            or (abs(float(new_temp) - float(last_temp)) >= float(self.log_temp_delta))
                            or (dt >= float(self.log_temp_max_sec))
                        ):
                            diffs.append({"cat": "telemetry", "field": "temp", "old": last_temp, "new": float(new_temp)})
                            rec["temp"] = float(new_temp)
                            rec["ts"] = now2
                    if new_rh is not None:
                        dt = now2 - last_ts if last_ts else 1e9
                        if self._log_enabled("DEBUG") and (
                            (last_rh is None)
                            or (abs(float(new_rh) - float(last_rh)) >= float(self.log_rh_delta))
                            or (dt >= float(self.log_rh_max_sec))
                        ):
                            diffs.append({"cat": "telemetry", "field": "rh", "old": last_rh, "new": float(new_rh)})
                            rec["rh"] = float(new_rh)
                            rec["ts"] = now2
                    if rec:
                        self._last_temp_log[str(tid)] = rec
                except Exception:
                    pass

            try:
                self._merge_static_from_source(tid, data)
            except Exception:
                pass

            self._sync_ui()
            self._persist_rt_cache()
            try:
                for d0 in diffs:
                    cat = str(d0.get("cat") or "")
                    field = str(d0.get("field") or "")
                    newv = d0.get("new")
                    # In MIN: log only meaningful state changes, and use ACK for commands.
                    if cat == "telemetry" and not self._log_enabled("DEBUG"):
                        continue
                    if cat == "state":
                        if field == "setpoint":
                            self._maybe_ack(
                                tid=str(tid),
                                field="setpoint",
                                new_value=newv,
                                name=str(t.get("name") or f"vTherm {tid}"),
                                source_num=num,
                            )
                        if field == "season":
                            self._maybe_ack(
                                tid=str(tid),
                                field="season",
                                new_value=newv,
                                name=str(t.get("name") or f"vTherm {tid}"),
                                source_num=num,
                            )
                        if field == "mode":
                            self._maybe_ack(
                                tid=str(tid),
                                field="mode",
                                new_value=newv,
                                name=str(t.get("name") or f"vTherm {tid}"),
                                source_num=num,
                            )
                        # Log state diffs from e-safe only in NORMAL/DEBUG, to reduce noise in MIN.
                        if not self._log_enabled("NORMAL"):
                            continue

                    self._log_event(
                        origin="esafe",
                        tid=str(tid),
                        name=str(t.get("name") or f"vTherm {tid}"),
                        source_num=num,
                        category=cat,
                        field=field,
                        old=d0.get("old"),
                        new=newv,
                        msg="update from e-safe",
                    )
            except Exception:
                pass
            return

    # -------------------- UI sync + discovery --------------------

    def _device_block(self, tid: str, name: str) -> Dict[str, Any]:
        return {
            "identifiers": [f"e_therm_plus_ks_{tid}"],
            "name": f"e-Therm {name}",
            "manufacturer": "Ekonex",
            "model": "e-Therm Plus KS",
        }

    def _sync_ui(self):
        rt_items = []
        st_items = []

        for t in self.therm_list():
            tid = str(t.get("id"))
            name = t.get("name") or f"e-Therm {tid}"

            rt = self.rt.get(tid, {})
            rt_item = {"ID": int(tid) if tid.isdigit() else tid, "DES": name}
            rt_item.update(rt)
            rt_items.append(rt_item)

            st = self._get_therm_static(tid)
            st_item = {"ID": int(tid) if tid.isdigit() else tid, "DES": name}
            st_item.update(st)
            st_items.append(st_item)

        self.state.apply_realtime_update("thermostats", rt_items)
        self.state.apply_static_update("thermostats", st_items)

        for t in self.therm_list():
            tid = str(t.get("id"))
            try:
                if not self._display_only_for(t):
                    self._ha_publish_clone_state(tid)
            except Exception:
                pass
            try:
                if self._display_only_for(t):
                    continue
                if self._is_split_outputs(t):
                    self._publish_outputs_state(t, "heat")
                    self._publish_outputs_state(t, "cool")
                else:
                    self._publish_outputs_state(t)
            except Exception:
                pass
        try:
            self._publish_pdc_consensus()
        except Exception:
            pass

    def _publish_discovery(self):
        try:
            self._last_discovery_publish_ts = time.time()
        except Exception:
            pass
        base = "homeassistant"
        # General PDC consensus switch
        pdc_uid = "e_therm_pdc"
        pdc_topic = f"{base}/switch/{pdc_uid}/config"
        pdc_dev = {
            "identifiers": ["e_therm_pdc"],
            "name": "e-therm PDC",
            "manufacturer": "Ekonex",
            "model": "e-Therm Plus KS",
        }
        pdc_cfg = {
            "name": "e-Therm PDC Consenso",
            "unique_id": pdc_uid,
            "availability_topic": f"{self.out_prefix}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "command_topic": f"{self.out_prefix}/pdc/set",
            "state_topic": f"{self.out_prefix}/pdc/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": pdc_dev,
            "icon": "mdi:hvac",
        }
        self.mqtt.publish(pdc_topic, json.dumps(pdc_cfg, ensure_ascii=False), retain=True)

        pdc_heat_uid = "e_therm_pdc_heat"
        pdc_heat_topic = f"{base}/switch/{pdc_heat_uid}/config"
        pdc_heat_cfg = {
            "name": "e-Therm PDC Heat",
            "unique_id": pdc_heat_uid,
            "availability_topic": f"{self.out_prefix}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "command_topic": f"{self.out_prefix}/pdc/heat/set",
            "state_topic": f"{self.out_prefix}/pdc/heat/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": pdc_dev,
            "icon": "mdi:radiator",
        }
        self.mqtt.publish(pdc_heat_topic, json.dumps(pdc_heat_cfg, ensure_ascii=False), retain=True)

        pdc_cool_uid = "e_therm_pdc_cool"
        pdc_cool_topic = f"{base}/switch/{pdc_cool_uid}/config"
        pdc_cool_cfg = {
            "name": "e-Therm PDC Cool",
            "unique_id": pdc_cool_uid,
            "availability_topic": f"{self.out_prefix}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "command_topic": f"{self.out_prefix}/pdc/cool/set",
            "state_topic": f"{self.out_prefix}/pdc/cool/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": pdc_dev,
            "icon": "mdi:snowflake",
        }
        self.mqtt.publish(pdc_cool_topic, json.dumps(pdc_cool_cfg, ensure_ascii=False), retain=True)

        # HA-climate sourced thermostats PDC consensus switch group
        pdc_ha_dev = {
            "identifiers": ["e_therm_pdc_ha"],
            "name": "e-therm PDC HA",
            "manufacturer": "Ekonex",
            "model": "e-Therm Plus KS",
        }
        pdc_ha_uid = "e_therm_pdc_ha"
        pdc_ha_topic = f"{base}/switch/{pdc_ha_uid}/config"
        pdc_ha_cfg = {
            "name": "e-Therm PDC HA Consenso",
            "unique_id": pdc_ha_uid,
            "availability_topic": f"{self.out_prefix}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "command_topic": f"{self.out_prefix}/pdc/ha/set",
            "state_topic": f"{self.out_prefix}/pdc/ha/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": pdc_ha_dev,
            "icon": "mdi:hvac",
        }
        self.mqtt.publish(pdc_ha_topic, json.dumps(pdc_ha_cfg, ensure_ascii=False), retain=True)

        pdc_ha_heat_uid = "e_therm_pdc_ha_heat"
        pdc_ha_heat_topic = f"{base}/switch/{pdc_ha_heat_uid}/config"
        pdc_ha_heat_cfg = {
            "name": "e-Therm PDC HA Heat",
            "unique_id": pdc_ha_heat_uid,
            "availability_topic": f"{self.out_prefix}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "command_topic": f"{self.out_prefix}/pdc/ha/heat/set",
            "state_topic": f"{self.out_prefix}/pdc/ha/heat/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": pdc_ha_dev,
            "icon": "mdi:radiator",
        }
        self.mqtt.publish(pdc_ha_heat_topic, json.dumps(pdc_ha_heat_cfg, ensure_ascii=False), retain=True)

        pdc_ha_cool_uid = "e_therm_pdc_ha_cool"
        pdc_ha_cool_topic = f"{base}/switch/{pdc_ha_cool_uid}/config"
        pdc_ha_cool_cfg = {
            "name": "e-Therm PDC HA Cool",
            "unique_id": pdc_ha_cool_uid,
            "availability_topic": f"{self.out_prefix}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "command_topic": f"{self.out_prefix}/pdc/ha/cool/set",
            "state_topic": f"{self.out_prefix}/pdc/ha/cool/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": pdc_ha_dev,
            "icon": "mdi:snowflake",
        }
        self.mqtt.publish(pdc_ha_cool_topic, json.dumps(pdc_ha_cool_cfg, ensure_ascii=False), retain=True)

        # Dynamic user-defined consensus groups.
        pdc_groups_dev = {
            "identifiers": ["e_therm_pdc_groups"],
            "name": "e-therm PDC Groups",
            "manufacturer": "Ekonex",
            "model": "e-Therm Plus KS",
        }
        groups: Dict[str, str] = {}
        for t in self.therm_list():
            g_heat = str(t.get("consensus_group_heat") or t.get("consensus_group") or t.get("pdc_group") or "").strip()
            g_cool = str(t.get("consensus_group_cool") or t.get("consensus_group") or t.get("pdc_group") or "").strip()
            if g_heat:
                g_key = _topic_safe_name(g_heat).lower()
                if g_key not in groups:
                    groups[g_key] = g_heat
            if g_cool:
                g_key = _topic_safe_name(g_cool).lower()
                if g_key not in groups:
                    groups[g_key] = g_cool
        # Add configured groups (even if no thermostat references them yet).
        cfg_groups = self.cfg.get("consensus_groups") if isinstance(self.cfg, dict) else []
        if isinstance(cfg_groups, list):
            for g in cfg_groups:
                if not isinstance(g, dict):
                    continue
                g_label = str(g.get("name") or "").strip()
                if not g_label:
                    continue
                g_key = _topic_safe_name(g_label).lower()
                if g_key not in groups:
                    groups[g_key] = g_label
        for g_key, g_label in groups.items():
            g_uid = f"e_therm_pdc_group_{g_key}"
            g_topic = f"{base}/switch/{g_uid}/config"
            g_cfg = {
                "name": f"PDC {g_label} Consenso",
                "unique_id": g_uid,
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "command_topic": f"{self.out_prefix}/pdc/groups/{g_key}/set",
                "state_topic": f"{self.out_prefix}/pdc/groups/{g_key}/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": pdc_groups_dev,
                "icon": "mdi:hvac",
            }
            self.mqtt.publish(g_topic, json.dumps(g_cfg, ensure_ascii=False), retain=True)

            g_heat_uid = f"e_therm_pdc_group_{g_key}_heat"
            g_heat_topic = f"{base}/switch/{g_heat_uid}/config"
            g_heat_cfg = {
                "name": f"PDC {g_label} Heat",
                "unique_id": g_heat_uid,
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "command_topic": f"{self.out_prefix}/pdc/groups/{g_key}/heat/set",
                "state_topic": f"{self.out_prefix}/pdc/groups/{g_key}/heat/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": pdc_groups_dev,
                "icon": "mdi:radiator",
            }
            self.mqtt.publish(g_heat_topic, json.dumps(g_heat_cfg, ensure_ascii=False), retain=True)

            g_cool_uid = f"e_therm_pdc_group_{g_key}_cool"
            g_cool_topic = f"{base}/switch/{g_cool_uid}/config"
            g_cool_cfg = {
                "name": f"PDC {g_label} Cool",
                "unique_id": g_cool_uid,
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "command_topic": f"{self.out_prefix}/pdc/groups/{g_key}/cool/set",
                "state_topic": f"{self.out_prefix}/pdc/groups/{g_key}/cool/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": pdc_groups_dev,
                "icon": "mdi:snowflake",
            }
            self.mqtt.publish(g_cool_topic, json.dumps(g_cool_cfg, ensure_ascii=False), retain=True)

        for t in self.therm_list():
            tid = str(t.get("id"))
            name = t.get("name") or f"e-Therm {tid}"
            if self._display_only_for(t):
                for tp in self._discovery_topics_for_any(t):
                    try:
                        self.mqtt.publish(tp, "", retain=True)
                    except Exception:
                        pass
                continue
            outputs = t.get("outputs") or {}
            heat_out = t.get("outputs_heat") if isinstance(t.get("outputs_heat"), dict) else None
            cool_out = t.get("outputs_cool") if isinstance(t.get("outputs_cool"), dict) else None
            allowed_modes = self._therm_allowed_modes(t)
            if "heat" not in allowed_modes:
                heat_out = {}
            if "cool" not in allowed_modes:
                cool_out = {}
            dev = self._device_block(tid, name)

            # MQTT climate clone. v4 uses the thermostat name for the stable
            # HA entity_id, e.g. climate.e_therm_ufficio.
            name_slug = _entity_safe_name(name, f"thermostat_{tid}")
            climate_uid = f"e_therm_{name_slug}_climate"
            climate_topic = f"{base}/climate/{climate_uid}/config"
            climate_modes = allowed_modes
            climate_min_temp, climate_max_temp = self._therm_temp_bounds(t)
            climate_cfg = {
                "name": f"e-Therm {name}",
                "unique_id": climate_uid,
                "object_id": f"e_therm_{name_slug}",
                "default_entity_id": f"climate.e_therm_{name_slug}",
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": dev,
                "mode_state_topic": f"{self._ha_base(tid)}/mode",
                "mode_command_topic": f"{self._ha_base(tid)}/mode/set",
                "temperature_state_topic": f"{self._ha_base(tid)}/target_temperature",
                "temperature_command_topic": f"{self._ha_base(tid)}/target_temperature/set",
                "current_temperature_topic": f"{self._ha_base(tid)}/current_temperature",
                "preset_mode_state_topic": f"{self._ha_base(tid)}/preset_mode",
                "preset_mode_command_topic": f"{self._ha_base(tid)}/preset_mode/set",
                "preset_modes": ["OFF", "MAN", "MAN_TMR", "WEEKLY", "AUTO", "SD1", "SD2"],
                "modes": climate_modes,
                "min_temp": climate_min_temp,
                "max_temp": climate_max_temp,
                "temp_step": 0.1,
            }
            self.mqtt.publish(climate_topic, json.dumps(climate_cfg, ensure_ascii=False), retain=True)
            self._remember_discovery_topic(tid, climate_topic)
            # Cleanup legacy discovery topics so HA does not keep reviving old aliases.
            try:
                for legacy_uid in (
                    f"e_therm_{tid}_climate",
                    f"e_therm_{tid}_climate_v2",
                    f"e_therm_{tid}_climate_v3",
                    f"e_therm_{tid}_climate_v4",
                ):
                    legacy_topic = f"{base}/climate/{legacy_uid}/config"
                    self.mqtt.publish(legacy_topic, "", retain=True)
            except Exception:
                pass

            # Humidity sensor for convenience
            hum_uid = f"e_therm_{tid}_humidity"
            hum_topic = f"{base}/sensor/{hum_uid}/config"
            hum_cfg = {
                "name": f"{name} Umidit?",
                "unique_id": hum_uid,
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": dev,
                "state_topic": f"{self._ha_base(tid)}/humidity",
                "unit_of_measurement": "%",
            }
            self.mqtt.publish(hum_topic, json.dumps(hum_cfg, ensure_ascii=False), retain=True)

            # Valve switch (state mirror)
            valv_uid = f"e_therm_{tid}_valv"
            valv_topic = f"{base}/switch/{valv_uid}/config"
            valv_cfg = {
                "name": f"{name} Valv",
                "unique_id": valv_uid,
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "command_topic": f"{self.out_prefix}/valv/{tid}/set",
                "state_topic": f"{self.out_prefix}/valv/{tid}/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": dev,
            }
            self.mqtt.publish(valv_topic, json.dumps(valv_cfg, ensure_ascii=False), retain=True)

            # Valve HOT (alta temperatura)
            valv_hot_uid = f"e_therm_{tid}_valv_hot"
            valv_hot_topic = f"{base}/switch/{valv_hot_uid}/config"
            valv_hot_cfg = {
                "name": f"{name} Valv Alta",
                "unique_id": valv_hot_uid,
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "command_topic": f"{self.out_prefix}/valv_hot/{tid}/set",
                "state_topic": f"{self.out_prefix}/valv_hot/{tid}/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": dev,
            }
            self.mqtt.publish(valv_hot_topic, json.dumps(valv_hot_cfg, ensure_ascii=False), retain=True)

            # Valve LOW (bassa temperatura)
            valv_low_uid = f"e_therm_{tid}_valv_low"
            valv_low_topic = f"{base}/switch/{valv_low_uid}/config"
            valv_low_cfg = {
                "name": f"{name} Valv Bassa",
                "unique_id": valv_low_uid,
                "availability_topic": f"{self.out_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "command_topic": f"{self.out_prefix}/valv_low/{tid}/set",
                "state_topic": f"{self.out_prefix}/valv_low/{tid}/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": dev,
            }
            self.mqtt.publish(valv_low_topic, json.dumps(valv_low_cfg, ensure_ascii=False), retain=True)

            # Clear any retained command topics from previous versions
            try:
                self.mqtt.publish(f"{self.out_prefix}/valv/{tid}/set", "", retain=True)
                self.mqtt.publish(f"{self.out_prefix}/valv_hot/{tid}/set", "", retain=True)
                self.mqtt.publish(f"{self.out_prefix}/valv_low/{tid}/set", "", retain=True)
            except Exception:
                pass

            # Outputs discovery:
            # - legacy: e-therm/thermostats/<id>/power + /fan/<sp>
            # - split:  e-therm/thermostats/<id>/<heat|cool>/power + /fan/<sp>
            if isinstance(heat_out, dict) or isinstance(cool_out, dict):
                heat_out = heat_out or {}
                cool_out = cool_out or {}
                if heat_out.get("power"):
                    uid = f"e_therm_{tid}_heat_power"
                    topic = f"{base}/number/{uid}/config"
                    cfg = {
                        "name": f"{name} Heat Power",
                        "unique_id": uid,
                        "availability_topic": f"{self.out_prefix}/status",
                        "payload_available": "online",
                        "payload_not_available": "offline",
                        "command_topic": f"{self.out_prefix}/thermostats/{tid}/heat/power/set",
                        "state_topic": f"{self.out_prefix}/thermostats/{tid}/heat/power",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "mode": "slider",
                        "device": dev,
                    }
                    self.mqtt.publish(topic, json.dumps(cfg, ensure_ascii=False), retain=True)
                if heat_out.get("fan3"):
                    for sp in ["min", "med", "max"]:
                        uid = f"e_therm_{tid}_heat_fan_{sp}"
                        topic = f"{base}/switch/{uid}/config"
                        cfg = {
                            "name": f"{name} Heat Fan {sp.upper()}",
                            "unique_id": uid,
                            "availability_topic": f"{self.out_prefix}/status",
                            "payload_available": "online",
                            "payload_not_available": "offline",
                            "command_topic": f"{self.out_prefix}/thermostats/{tid}/heat/fan/{sp}/set",
                            "state_topic": f"{self.out_prefix}/thermostats/{tid}/heat/fan/{sp}",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "device": dev,
                        }
                        self.mqtt.publish(topic, json.dumps(cfg, ensure_ascii=False), retain=True)

                if cool_out.get("power"):
                    uid = f"e_therm_{tid}_cool_power"
                    topic = f"{base}/number/{uid}/config"
                    cfg = {
                        "name": f"{name} Cool Power",
                        "unique_id": uid,
                        "availability_topic": f"{self.out_prefix}/status",
                        "payload_available": "online",
                        "payload_not_available": "offline",
                        "command_topic": f"{self.out_prefix}/thermostats/{tid}/cool/power/set",
                        "state_topic": f"{self.out_prefix}/thermostats/{tid}/cool/power",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "mode": "slider",
                        "device": dev,
                    }
                    self.mqtt.publish(topic, json.dumps(cfg, ensure_ascii=False), retain=True)
                if cool_out.get("fan3"):
                    for sp in ["min", "med", "max"]:
                        uid = f"e_therm_{tid}_cool_fan_{sp}"
                        topic = f"{base}/switch/{uid}/config"
                        cfg = {
                            "name": f"{name} Cool Fan {sp.upper()}",
                            "unique_id": uid,
                            "availability_topic": f"{self.out_prefix}/status",
                            "payload_available": "online",
                            "payload_not_available": "offline",
                            "command_topic": f"{self.out_prefix}/thermostats/{tid}/cool/fan/{sp}/set",
                            "state_topic": f"{self.out_prefix}/thermostats/{tid}/cool/fan/{sp}",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "device": dev,
                        }
                        self.mqtt.publish(topic, json.dumps(cfg, ensure_ascii=False), retain=True)
            else:
                if outputs.get("power"):
                    uid = f"e_therm_{tid}_power"
                    topic = f"{base}/number/{uid}/config"
                    cfg = {
                        "name": f"{name} Power",
                        "unique_id": uid,
                        "availability_topic": f"{self.out_prefix}/status",
                        "payload_available": "online",
                        "payload_not_available": "offline",
                        "command_topic": f"{self.out_prefix}/thermostats/{tid}/power/set",
                        "state_topic": f"{self.out_prefix}/thermostats/{tid}/power",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "mode": "slider",
                        "device": dev,
                    }
                    self.mqtt.publish(topic, json.dumps(cfg, ensure_ascii=False), retain=True)

                if outputs.get("fan3"):
                    for sp in ["min", "med", "max"]:
                        uid = f"e_therm_{tid}_fan_{sp}"
                        topic = f"{base}/switch/{uid}/config"
                        cfg = {
                            "name": f"{name} Fan {sp.upper()}",
                            "unique_id": uid,
                            "availability_topic": f"{self.out_prefix}/status",
                            "payload_available": "online",
                            "payload_not_available": "offline",
                            "command_topic": f"{self.out_prefix}/thermostats/{tid}/fan/{sp}/set",
                            "state_topic": f"{self.out_prefix}/thermostats/{tid}/fan/{sp}",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "device": dev,
                        }
                        self.mqtt.publish(topic, json.dumps(cfg, ensure_ascii=False), retain=True)

    # -------------------- UI commands --------------------

    def handle_ui_command(self, cmd: Dict[str, Any]):
        # config save
        if cmd.get("type") == "vtherm_config" and cmd.get("action") == "save":
            self.apply_config(cmd.get("value") or {})
            return {"ok": True}

        # MQTT discovery maintenance from UI
        if cmd.get("type") == "mqtt":
            action = str(cmd.get("action") or "").strip().lower()
            if action == "cleanup_discovery":
                topics = self._discovery_topics_full_cleanup(128)
                self._cleanup_discovery_topics(topics)
                return {"ok": True, "cleaned": len(topics)}
            if action == "republish_discovery":
                self._publish_discovery()
                return {"ok": True}
            return {"ok": False, "error": "unsupported_mqtt_action"}

        if cmd.get("type") == "computherm":
            action = str(cmd.get("action") or "").strip().lower()
            if action == "refresh":
                try:
                    return self._computherm_poll_once()
                except Exception as e:
                    self._computherm_last_error = str(e)
                    self._computherm_state_meta("error")
                    return {"ok": False, "error": str(e)}
            return {"ok": False, "error": "unsupported_computherm_action"}

        # test helper for /logs
        if cmd.get("type") == "e_therm" and cmd.get("action") == "log_test":
            try:
                self._log_event(
                    origin="ui",
                    tid=str(cmd.get("id")) if cmd.get("id") is not None else None,
                    name=None,
                    source_num=None,
                    category="test",
                    field="ping",
                    old=None,
                    new=None,
                    msg="test log button",
                )
            except Exception:
                pass
            return {"ok": True}

        if cmd.get("type") != "thermostats":
            return {"ok": False, "error": "unsupported_type"}

        tid = str(cmd.get("id"))
        action = str(cmd.get("action") or "")
        value = cmd.get("value")
        is_guest_cmd = (
            str(cmd.get("source") or "").strip().lower() == "guest"
            or str(cmd.get("origin") or "").strip().lower() == "guest"
            or bool(cmd.get("guest"))
        )

        def _snapshot_therm_info(therm_id: str) -> Dict[str, Any]:
            info: Dict[str, Any] = {"name": None, "target": None, "source_num": None}
            try:
                snap = self.state.snapshot()
                for ent in snap.get("entities") or []:
                    if str(ent.get("type") or "").lower() != "thermostats":
                        continue
                    if str(ent.get("id")) != str(therm_id):
                        continue
                    st = ent.get("static") if isinstance(ent.get("static"), dict) else {}
                    rt = ent.get("realtime") if isinstance(ent.get("realtime"), dict) else {}
                    therm = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}
                    thr = therm.get("TEMP_THR") if isinstance(therm.get("TEMP_THR"), dict) else {}
                    info["name"] = ent.get("name") or st.get("DES")
                    info["target"] = thr.get("VAL")
                    try:
                        info["source_num"] = int(ent.get("source_num") or st.get("NUM") or therm_id)
                    except Exception:
                        info["source_num"] = None
                    break
            except Exception:
                pass
            return info

        # Map UI actions to HA clone command handler where possible
        if action == "set_target":
            old_info = _snapshot_therm_info(tid) if is_guest_cmd else {}
            self._handle_ha_clone_command(tid, "target_temperature", str(value), origin="ui")
            if is_guest_cmd:
                try:
                    client = cmd.get("client") if isinstance(cmd.get("client"), dict) else {}
                    guest_room = str(cmd.get("guest_room") or "").strip()
                    ip = str(client.get("ip") or "").strip()
                    ua = str(client.get("user_agent") or "").strip()
                    self._log_event(
                        origin="guest",
                        tid=tid,
                        name=old_info.get("name"),
                        source_num=old_info.get("source_num"),
                        category="guest",
                        field="setpoint",
                        old=old_info.get("target"),
                        new=value,
                        msg=(
                            f"setpoint guest {guest_room}".strip()
                            + (f" da {ip}" if ip else "")
                        ),
                        extra={
                            "guest_room": guest_room,
                            "client": client,
                            "device": ua,
                            "page": cmd.get("page"),
                        },
                    )
                except Exception:
                    pass
            return {"ok": True}
        if action == "set_mode":
            v = str(value or "").strip().upper()
            if v == "OFF":
                self._handle_ha_clone_command(tid, "mode", "off", origin="ui")
            else:
                self._handle_ha_clone_command(tid, "preset_mode", v, origin="ui")
            return {"ok": True}
        if action == "set_season":
            v = str(value or "").strip().upper()
            if v == "WIN":
                self._handle_ha_clone_command(tid, "mode", "heat", origin="ui")
            elif v == "SUM":
                self._handle_ha_clone_command(tid, "mode", "cool", origin="ui")
            elif v == "OFF":
                self._handle_ha_clone_command(tid, "mode", "off", origin="ui")
            return {"ok": True}

        # Local-only persistence for profile/schedule unless we know official e-safe command topics
        if action == "set_profile":
            if not isinstance(value, dict):
                return {"ok": False, "error": "invalid_value"}
            season = str(value.get("season") or "WIN").upper()
            key = str(value.get("key") or "").upper()
            val = _as_float(value.get("value"))
            if season not in ("WIN", "SUM") or key not in ("T1", "T2", "T3", "TM") or val is None:
                return {"ok": False, "error": "invalid_value"}
            st = self._get_therm_static(tid)
            st[season][key] = float(val)
            self._set_therm_static(tid, st)
            self._sync_ui()
            return {"ok": True}

        if action == "set_schedule":
            if not isinstance(value, dict):
                return {"ok": False, "error": "invalid_value"}
            season = str(value.get("season") or "WIN").upper()
            day = str(value.get("day") or "MON").upper()
            hour = _as_int(value.get("hour"))
            tsel = str(value.get("t") or "").strip()
            if season not in ("WIN", "SUM") or day not in DAYS or hour is None or hour < 0 or hour > 23:
                return {"ok": False, "error": "invalid_value"}
            if tsel not in ("1", "2", "3"):
                return {"ok": False, "error": "invalid_value"}
            st = self._get_therm_static(tid)
            st[season][day][int(hour)] = {"T": tsel}
            self._set_therm_static(tid, st)
            self._sync_ui()
            return {"ok": True}

        return {"ok": False, "error": "unsupported_action"}


def main():
    opts = load_options()
    state = LaresState()
    engine = ThermEngine(state, opts)

    cfg = load_config() or {}
    try:
        state.set_meta("vtherm_config", cfg)
    except Exception:
        pass
    engine.apply_config(cfg)

    set_command_handler(lambda cmd: engine.handle_ui_command(cmd))
    engine.connect()
    engine.start_control()
    engine.start_computherm()
    engine.start_watchdog()

    start_debug_server(state, host="0.0.0.0", port=8080, command_fn=engine.handle_ui_command)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
