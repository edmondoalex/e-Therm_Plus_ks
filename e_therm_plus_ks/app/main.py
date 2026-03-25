import json
import os
import time
import threading
import warnings
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, List

import paho.mqtt.client as mqtt

from debug_server import LaresState, start_debug_server, set_command_handler
from pwm_controller import PWMController

CONFIG_PATH = "/data/vtherm.json"
RUNTIME_PATH = "/data/vtherm_runtime.json"
EVENTS_PATH = "/data/e_therm_events.jsonl"
APP_VERSION = "2.6.56"
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
        self._last_ha_poll_ts = 0.0
        self._last_ha_warn_ts = 0.0

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
        self.pwm_min_to_med = int(opts.get("pwm_min_to_med", 34) or 34)
        self.pwm_med_to_max = int(opts.get("pwm_med_to_max", 67) or 67)
        self._pwm: Dict[str, PWMController] = {}
        self._manual_override_until: Dict[str, float] = {}
        self._manual_valve_until: Dict[str, float] = {}
        self._manual_valve_state: Dict[str, Dict[str, bool]] = {}
        self._real_target_last: Dict[str, Any] = {}
        self._control_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

        # event log (for /logs UI)
        self._events_lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
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
            except Exception:
                pass
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
        try:
            if isinstance(t, dict) and t.get("auto_control_enabled") is not None:
                return bool(t.get("auto_control_enabled"))
        except Exception:
            pass
        return bool(self.auto_control_enabled)

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
        if not force and (now - float(self._last_ha_poll_ts or 0.0)) < 5.0:
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
            preset = str(attrs.get("preset_mode") or "").strip().upper()

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
                out_status = "OFF" if hvac == "off" else "ON"
                th["OUT_STATUS"] = out_status

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

    def _ha_service_call(self, domain: str, service: str, data: Dict[str, Any]) -> bool:
        res = self._ha_api_request("POST", f"/services/{domain}/{service}", data or {})
        return res is not None

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
            self._real_target_last[cache_key] = desired

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

    def _apply_real_outputs(self, t: Dict[str, Any], desired: Dict[str, Any], outputs: Dict[str, Any], season_key: Optional[str] = None) -> None:
        targets = self._real_targets_for(t, season_key)
        if not isinstance(targets, dict):
            return

        if outputs.get("power"):
            pwm = int(desired.get("power", 0) or 0)
            pwm_light = str(
                targets.get("power_light")
                or targets.get("pwm_light")
                or targets.get("dimmer_light")
                or ""
            ).strip()
            if pwm_light:
                self._apply_real_pwm_light(pwm_light, pwm)

        if outputs.get("fan3"):
            fan = desired.get("fan") if isinstance(desired.get("fan"), dict) else {}
            fan_sw = targets.get("fan_switches") if isinstance(targets.get("fan_switches"), dict) else {}
            for sp in ("min", "med", "max"):
                ent = str(
                    fan_sw.get(sp)
                    or targets.get(f"fan_{sp}_switch")
                    or ""
                ).strip()
                if not ent:
                    continue
                on = str((fan or {}).get(sp, "OFF")).upper() == "ON"
                self._apply_real_switch(ent, on)

    def _apply_real_valve(self, t: Dict[str, Any], valv_on: bool) -> None:
        targets = self._real_targets_for(t, None)
        if not isinstance(targets, dict):
            return
        ent = str(targets.get("valve_switch") or targets.get("valv_switch") or "").strip()
        if ent:
            self._apply_real_switch(ent, bool(valv_on))

    def _apply_real_valves(self, t: Dict[str, Any], low_on: bool, hot_on: bool) -> None:
        targets = self._real_targets_for(t, None)
        if not isinstance(targets, dict):
            return
        ent_low = str(
            targets.get("valve_switch_low")
            or targets.get("valv_switch_low")
            or targets.get("valve_switch_bassa")
            or ""
        ).strip()
        ent_hot = str(
            targets.get("valve_switch_hot")
            or targets.get("valv_switch_hot")
            or targets.get("valve_switch_alta")
            or ""
        ).strip()
        if ent_low:
            self._apply_real_switch(ent_low, bool(low_on))
        if ent_hot:
            self._apply_real_switch(ent_hot, bool(hot_on))
        # Back-compat: drive single valve if configured
        ent = str(targets.get("valve_switch") or targets.get("valv_switch") or "").strip()
        if ent:
            self._apply_real_switch(ent, bool(low_on or hot_on))

    def _discovery_topics_for_therm(self, tid: str, outputs: Dict[str, Any]) -> List[str]:
        base = "homeassistant"
        topics = [
            f"{base}/climate/e_therm_{tid}_climate/config",
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
            return self._discovery_topics_for_therm_split(
                tid,
                t.get("outputs_heat") or {},
                t.get("outputs_cool") or {},
            )
        return self._discovery_topics_for_therm(tid, (t.get("outputs") or {}))

    def _is_split_outputs(self, t: Dict[str, Any]) -> bool:
        try:
            return isinstance(t.get("outputs_heat"), dict) or isinstance(t.get("outputs_cool"), dict)
        except Exception:
            return False

    def _season_key_from_act_sea(self, act_sea: Any) -> str:
        return "cool" if str(act_sea or "").upper() == "SUM" else "heat"

    def _outputs_for_season(self, t: Dict[str, Any], season_key: str) -> Dict[str, Any]:
        if not self._is_split_outputs(t):
            return t.get("outputs") or {}
        if str(season_key) == "cool":
            return t.get("outputs_cool") or {}
        return t.get("outputs_heat") or {}

    def _discovery_topics_for_therm_split(self, tid: str, heat_out: Dict[str, Any], cool_out: Dict[str, Any]) -> List[str]:
        base = "homeassistant"
        topics = [
            f"{base}/climate/e_therm_{tid}_climate/config",
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

        # Cleanup discovery for removed thermostats or removed outputs.
        to_cleanup: List[str] = []
        for tid, old_t in old_by_id.items():
            if tid not in new_by_id:
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
            save_config(self.cfg)
        try:
            self.state.set_meta("vtherm_config", self.cfg)
        except Exception:
            pass
        self._sync_ui()
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
            time.sleep(5)

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

        # If MQTT reports disconnected, attempt reconnect with backoff.
        if not bool(self._mqtt_connected):
            self._reconnect_mqtt("mqtt_not_connected")
            return

        # Only check "stale source" if we have received at least one source message before.
        if cfg_has_therms and self._ever_got_source:
            last_src = float(self._last_source_ts or 0.0)
            if last_src and (now - last_src) > float(stale_sec):
                self._reconnect_mqtt(f"stale_source>{stale_sec}s")



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
        if (now - last) < float(self.opts.get("control_interval_sec", 5) or 5):
            return
        self.runtime["_last_control_ts"] = now

        # Keep HA climate-backed thermostats in sync even when watchdog is disabled.
        try:
            self._poll_ha_climate_states()
        except Exception:
            pass

        for t in self.therm_list():
            try:
                if not self._auto_enabled_for(t):
                    continue
                self._control_one(t, now)
            except Exception:
                continue

        try:
            save_runtime(self.runtime)
        except Exception:
            pass

    def _control_one(self, t: Dict[str, Any], now: float) -> None:
        tid = str(t.get("id"))
        split = self._is_split_outputs(t)
        # use active season outputs when split, otherwise legacy
        with self.lock:
            rt0 = self.rt.get(tid) or {}
            th0 = rt0.get("THERM") if isinstance(rt0.get("THERM"), dict) else {}
            sea0 = th0.get("ACT_SEA") if isinstance(th0, dict) else None
        active_sk = self._season_key_from_act_sea(sea0)
        outputs = self._outputs_for_season(t, active_sk) if split else (t.get("outputs") or {})
        if not (outputs.get("power") or outputs.get("fan3")):
            return

        # manual override window
        ov_key = f"{tid}:{active_sk}" if split else tid
        until = float(self._manual_override_until.get(ov_key, 0.0) or 0.0)
        if until and now < until:
            return

        with self.lock:
            rt = self.rt.get(tid) or {}
            th = rt.get("THERM") if isinstance(rt.get("THERM"), dict) else {}

        cur = rt.get("TEMP")
        if cur is None:
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
            return

        # OFF => outputs off
        if model == "OFF":
            pwm = 0
        else:
            # deadband
            if sea == "SUM":
                err = cur_f - float(setp)
            else:
                err = float(setp) - cur_f
            if abs(err) < float(self.pwm_deadband):
                pwm = 0
            else:
                c = self._get_pwm_controller(tid)
                if sea == "SUM":
                    pwm = c.compute_pwm(cur_f, float(setp), now=now)
                else:
                    pwm = c.compute_pwm(float(setp), cur_f, now=now)

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
                self._publish_outputs_state(t, inactive_sk)
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
        rc = args[2] if len(args) > 2 else kwargs.get("rc", 0)
        self._mqtt_connected = False
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
        try:
            if client is not None:
                client.publish(f"{self.out_prefix}/status", "offline", retain=True)
        except Exception:
            pass

    def _on_connect(self, *args, **kwargs):
        client = args[0] if len(args) > 0 else None
        flags = args[2] if len(args) > 2 else kwargs.get("flags", {})
        rc = args[3] if len(args) > 3 else kwargs.get("rc", 0)
        self._mqtt_connected = True
        try:
            self._reconnect_backoff_sec = 5.0
            self._last_mqtt_any_ts = time.time()
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
        self._clear_retained_valve_commands()
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
        client.subscribe(f"{self.out_prefix}/valv/+/set", qos=0)
        client.subscribe(f"{self.out_prefix}/valv_hot/+/set", qos=0)
        client.subscribe(f"{self.out_prefix}/valv_low/+/set", qos=0)

        self._sync_ui()
        try:
            self._poll_ha_climate_states(force=True)
        except Exception:
            pass
        self._publish_discovery()
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

    def _publish_outputs_state(self, t: Dict[str, Any], season_key: Optional[str] = None) -> None:
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

    def _calc_auto_valves(self, t: Dict[str, Any]) -> tuple[bool, bool]:
        """Return (low_on, hot_on) for automatic logic."""
        tid = str(t.get("id"))
        demand = self._valve_on_for_therm(t)
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
            else:
                hot_on = True
                low_on = True
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
                if not self._valve_on_for_therm(t):
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
                if not self._valve_on_for_therm(t):
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
        try:
            cfg_groups = self.cfg.get("consensus_groups") if isinstance(self.cfg, dict) else []
            if not isinstance(cfg_groups, list):
                cfg_groups = []
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
                    self._apply_real_switch(sw, bool(st.get("on")))
                if sw_h:
                    self._apply_real_switch(sw_h, bool(st.get("on_heat")))
                if sw_c:
                    self._apply_real_switch(sw_c, bool(st.get("on_cool")))
        except Exception:
            pass

    # -------------------- HA clone (MQTT climate) --------------------

    def _ha_base(self, tid: str) -> str:
        return f"{self.out_prefix}/thermostats/{tid}"

    def _ha_publish_clone_state(self, tid: str) -> None:
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
        src = t.get("source") or {}
        stype = str(src.get("type", "")).lower()
        is_esafe = stype in ("esafe", "esafe_json")
        is_ha = stype in ("ha_climate", "homeassistant_climate", "ha")
        if not (is_esafe or is_ha):
            return
        num = None
        if is_esafe:
            try:
                num = int(src.get("num"))
            except Exception:
                return
        ent = str(src.get("entity_id") or "").strip() if is_ha else ""
        if is_ha and not ent:
            return
        name = str(t.get("name") or f"vTherm {tid}")

        # ensure rt/therm exist
        with self.lock:
            rt = self.rt.setdefault(str(tid), {})
            th = rt.setdefault("THERM", {})

        if kind == "target_temperature":
            v = _as_float(payload_raw)
            if v is None:
                return
            with self.lock:
                rt0 = self.rt.setdefault(str(tid), {})
                th0 = rt0.setdefault("THERM", {})
                old_v = None
                thr0 = th0.get("TEMP_THR") if isinstance(th0.get("TEMP_THR"), dict) else None
                if thr0 and thr0.get("VAL") is not None:
                    old_v = _as_float(thr0.get("VAL"))
            if is_esafe:
                self.mqtt.publish(f"{self.source_prefix}/cmd/thermostat/{num}/temperature", str(v), retain=False)
            else:
                self._ha_climate_service(ent, "set_temperature", {"temperature": float(v)})
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
            self._sync_ui()
            return

        if kind == "mode":
            m = str(payload_raw or "").strip().lower()
            if m not in ("heat", "cool", "off"):
                return
            with self.lock:
                rt0 = self.rt.setdefault(str(tid), {})
                th0 = rt0.setdefault("THERM", {})
                old_sea = str(th0.get("ACT_SEA") or "").upper() or None
            if is_esafe:
                self.mqtt.publish(f"{self.source_prefix}/cmd/thermostat/{num}/mode", m, retain=False)
            else:
                self._ha_climate_service(ent, "set_hvac_mode", {"hvac_mode": m})
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
                elif m == "cool":
                    th["ACT_SEA"] = "SUM"
            self._sync_ui()
            return

        if kind == "preset_mode":
            p = str(payload_raw or "").strip().upper()
            if not p:
                return
            with self.lock:
                rt0 = self.rt.setdefault(str(tid), {})
                th0 = rt0.setdefault("THERM", {})
                old_p = str(th0.get("ACT_MODEL") or th0.get("ACT_MODE") or "").upper() or None
            if is_esafe:
                self.mqtt.publish(f"{self.source_prefix}/cmd/thermostat/{num}/preset_mode", p, retain=False)
            else:
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
            self._sync_ui()
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
        on = str(payload_raw or "").strip().upper() in ("ON", "1", "TRUE", "YES")
        low_on, hot_on = self._calc_auto_valves(t)
        if kind == "valv":
            low_on = on
            hot_on = on
        elif kind == "valv_hot":
            hot_on = on
        elif kind == "valv_low":
            low_on = on

        self._manual_valve_state[str(tid)] = {"low": bool(low_on), "hot": bool(hot_on)}
        self._manual_valve_until[str(tid)] = time.time() + float(self._override_sec_for(t))
        self._publish_valve_state(t)
        return True

    def _clear_retained_valve_commands(self) -> None:
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

        if topic.startswith(f"{self.out_prefix}/thermostats/"):
            # Stability: ignore retained "command" messages (*/set) that might be left on the broker.
            # Otherwise on (re)subscribe we would apply an old command and trigger manual override,
            # which looks like "auto control blocked" for auto_override_sec seconds.
            try:
                if bool(getattr(msg, "retain", False)) and topic.endswith("/set"):
                    print(f"[WARN] Ignoring retained command on {topic}")
                    return
            except Exception:
                pass
            self._handle_out_prefix_command(topic, payload_raw)
            return

        if topic.startswith(f"{self.out_prefix}/valv") or topic.startswith(f"{self.out_prefix}/valv_hot") or topic.startswith(f"{self.out_prefix}/valv_low"):
            try:
                if bool(getattr(msg, "retain", False)) and topic.endswith("/set"):
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
                    th["ACT_SEA"] = str(season).upper()
                if model:
                    th["ACT_MODEL"] = str(model).upper()
                if out_status:
                    th["OUT_STATUS"] = str(out_status).upper()
                if target is not None:
                    v = _as_float(target)
                    if v is not None:
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

        try:
            for t in self.therm_list():
                tid = str(t.get("id"))
                if self._is_split_outputs(t):
                    self._publish_outputs_state(t, "heat")
                    self._publish_outputs_state(t, "cool")
                else:
                    self._publish_outputs_state(t)
                self._ha_publish_clone_state(tid)
            self._publish_pdc_consensus()
        except Exception:
            pass

    def _publish_discovery(self):
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
            outputs = t.get("outputs") or {}
            heat_out = t.get("outputs_heat") if isinstance(t.get("outputs_heat"), dict) else None
            cool_out = t.get("outputs_cool") if isinstance(t.get("outputs_cool"), dict) else None
            dev = self._device_block(tid, name)

            # MQTT climate clone of e-safe thermostat
            climate_uid = f"e_therm_{tid}_climate"
            climate_topic = f"{base}/climate/{climate_uid}/config"
            climate_cfg = {
                "name": name,
                "unique_id": climate_uid,
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
                "modes": ["off", "heat", "cool"],
                "min_temp": 5,
                "max_temp": 35,
                "temp_step": 0.1,
            }
            self.mqtt.publish(climate_topic, json.dumps(climate_cfg, ensure_ascii=False), retain=True)

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

        # Map UI actions to HA clone command handler where possible
        if action == "set_target":
            self._handle_ha_clone_command(tid, "target_temperature", str(value), origin="ui")
            return {"ok": True}
        if action == "set_mode":
            self._handle_ha_clone_command(tid, "preset_mode", str(value), origin="ui")
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
    engine.start_watchdog()

    start_debug_server(state, host="0.0.0.0", port=8080, command_fn=engine.handle_ui_command)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
