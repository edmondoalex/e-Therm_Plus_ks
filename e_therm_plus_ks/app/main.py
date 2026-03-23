import json
import os
import time
import threading
from typing import Any, Dict

import paho.mqtt.client as mqtt

from debug_server import LaresState, start_debug_server, set_command_handler

CONFIG_PATH = "/data/vtherm.json"

DEVICE_BLOCK = {
    "identifiers": ["e_therm_term_thermostats"],
    "name": "e-Therm Termostati",
    "manufacturer": "Ekonex",
    "model": "e-Therm Plus KS",
}

def load_options() -> Dict[str, Any]:
    try:
        with open("/data/options.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg: Dict[str, Any]) -> None:
    os.makedirs("/data", exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

class ThermEngine:
    def __init__(self, state: LaresState, opts: Dict[str, Any]):
        self.state = state
        self.opts = opts
        self.lock = threading.Lock()

        self.cfg = load_config() or {}
        self.source_prefix = str(opts.get("source_prefix", "e-safe")).strip().rstrip("/")
        self.out_prefix = str(opts.get("out_prefix", "e-therm")).strip().rstrip("/")
        self.control_interval = int(opts.get("control_interval_sec", 5) or 5)

        self.mqtt = mqtt.Client(client_id=f"e-therm-plus-{int(time.time())}")
        user = (opts.get("mqtt_user") or "").strip()
        pw = (opts.get("mqtt_password") or "")
        if user:
            self.mqtt.username_pw_set(user, pw)

        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message

        self.rt: Dict[str, Dict[str, Any]] = {}

    def therm_list(self):
        return (self.cfg.get("thermostats") or [])

    def apply_config(self, cfg: Dict[str, Any]):
        with self.lock:
            self.cfg = cfg or {}
            save_config(self.cfg)
        try:
            self.state.set_meta("vtherm_config", self.cfg)
        except Exception:
            pass
        self._sync_ui_thermostats()
        self._publish_discovery()

    def connect(self):
        host = str(self.opts.get("mqtt_host", "core-mosquitto"))
        port = int(self.opts.get("mqtt_port", 1883))
        try:
            self.mqtt.connect(host, port, 60)
            self.mqtt.loop_start()
        except Exception as e:
            print(f"[WARN] MQTT connect failed to {host}:{port} -> {e}")
            try:
                self.mqtt.loop_start()
            except Exception:
                pass

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(f"{self.source_prefix}/thermostats/+")
        client.subscribe(f"{self.source_prefix}/thermostats/+/+")
        self._sync_ui_thermostats()
        self._publish_discovery()
        client.publish(f"{self.out_prefix}/status", "online", retain=True)

    def _sync_ui_thermostats(self):
        items = []
        for t in self.therm_list():
            tid = str(t.get("id"))
            name = t.get("name") or f"e-Therm {tid}"
            rt = self.rt.get(tid, {})
            item = {"ID": int(tid) if tid.isdigit() else tid, "DES": name}
            item.update(rt)
            items.append(item)
        self.state.apply_realtime_update("thermostats", items)
        # Publish current state to MQTT so discovery-created entities have state.
        try:
            for t in self.therm_list():
                tid = str(t.get("id"))
                outputs = t.get("outputs") or {}
                rt = self.rt.get(tid, {})
                # Publish logical PWM/power value if thermostat exposes 'power' output
                if outputs.get("power"):
                    val = ""
                    thr = rt.get("TEMP_THR") or {}
                    if isinstance(thr, dict) and thr.get("VAL") is not None:
                        try:
                            val = str(float(thr.get("VAL")))
                        except Exception:
                            val = str(thr.get("VAL"))
                    self.mqtt.publish(f"{self.out_prefix}/thermostats/{tid}/power", val, retain=True)

                # Publish fan states (default OFF). Integration later will set real states.
                if outputs.get("fan3"):
                    for sp in ["min", "med", "max"]:
                        self.mqtt.publish(f"{self.out_prefix}/thermostats/{tid}/fan/{sp}", "OFF", retain=True)
        except Exception:
            # don't let publish errors break runtime
            pass

    def _find_by_source_num(self, num: int):
        for t in self.therm_list():
            src = t.get("source") or {}
            if str(src.get("type","")).lower() in ("esafe","esafe_json") and int(src.get("num", -1)) == int(num):
                return t
        return None

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload_raw = msg.payload.decode("utf-8", errors="ignore").strip()

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

        if key is None:
            try:
                data = json.loads(payload_raw or "{}")
            except Exception:
                return

            cur = data.get("TEMP")
            rh = data.get("RH")
            therm = data.get("THERM") or {}
            season = therm.get("ACT_SEA") or data.get("ACT_SEA")
            model  = therm.get("ACT_MODEL") or data.get("ACT_MODE")
            out_status = therm.get("OUT_STATUS") or data.get("OUT_STATUS")
            temp_thr = (therm.get("TEMP_THR") or {}).get("VAL")

            target = None
            if temp_thr is not None:
                target = temp_thr
            elif str(season).upper() == "WIN":
                target = (data.get("WIN") or {}).get("TM")
            elif str(season).upper() == "SUM":
                target = (data.get("SUM") or {}).get("TM")

            with self.lock:
                rt = self.rt.setdefault(tid, {})
                if cur is not None:
                    try: rt["TEMP"] = float(cur)
                    except: pass
                if rh is not None:
                    try: rt["RH"] = float(rh)
                    except: pass
                if target is not None:
                    try: rt["TEMP_THR"] = {"VAL": float(target)}
                    except: pass

                if season:
                    rt["ACT_SEA"] = str(season).upper()
                    if str(season).upper() == "WIN":
                        rt["ACT_MODE"] = "HEA" if str(out_status).upper() != "OFF" else "OFF"
                    elif str(season).upper() == "SUM":
                        rt["ACT_MODE"] = "COO" if str(out_status).upper() != "OFF" else "OFF"

                if model:
                    rt["ACT_MODEL"] = str(model).upper()
                if out_status:
                    rt["OUT_STATUS"] = str(out_status).upper()

            self._sync_ui_thermostats()
            return

        payload = payload_raw
        with self.lock:
            rt = self.rt.setdefault(tid, {})
            if key == "current_temperature":
                try: rt["TEMP"] = float(payload)
                except: pass
            elif key == "target_temperature":
                try: rt["TEMP_THR"] = {"VAL": float(payload)}
                except: pass
            elif key == "hvac_mode":
                mode = payload.lower()
                if mode == "heat":
                    rt["ACT_MODE"] = "HEA"
                    rt["ACT_SEA"] = "WIN"
                elif mode == "cool":
                    rt["ACT_MODE"] = "COO"
                    rt["ACT_SEA"] = "SUM"
                else:
                    rt["ACT_MODE"] = "OFF"
            elif key == "preset_mode":
                rt["ACT_MODEL"] = payload
            elif key == "action":
                rt["action"] = payload

        self._sync_ui_thermostats()

    def _publish_discovery(self):
        base = "homeassistant"
        for t in self.therm_list():
            tid = str(t.get("id"))
            name = t.get("name") or f"e-Therm {tid}"
            outputs = t.get("outputs") or {}

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
                    "device": DEVICE_BLOCK,
                }
                self.mqtt.publish(topic, json.dumps(cfg), retain=True)

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
                        "device": DEVICE_BLOCK,
                    }
                    self.mqtt.publish(topic, json.dumps(cfg), retain=True)

    def _esafe_cmd_topic(self, num: int, key: str) -> str:
        return f"{self.source_prefix}/cmd/thermostat/{num}/{key}"

    def handle_ui_command(self, cmd: Dict[str, Any]):
        if cmd.get("type") == "vtherm_config" and cmd.get("action") == "save":
            self.apply_config(cmd.get("value") or {})
            return

        if cmd.get("type") != "thermostats":
            return

        tid = str(cmd.get("id"))
        action = str(cmd.get("action") or "")
        value = cmd.get("value")

        t = next((x for x in self.therm_list() if str(x.get("id")) == tid), None)
        if not t:
            return
        src = t.get("source") or {}
        if str(src.get("type","")).lower() not in ("esafe","esafe_json"):
            return
        num = int(src.get("num"))

        if action == "set_target":
            try:
                val = float(value)
            except Exception:
                return
            self.mqtt.publish(self._esafe_cmd_topic(num, "temperature"), str(val), retain=False)
        elif action == "set_mode":
            mode = str(value or "").lower()
            self.mqtt.publish(self._esafe_cmd_topic(num, "mode"), mode, retain=False)
        elif action == "set_profile":
            self.mqtt.publish(self._esafe_cmd_topic(num, "preset_mode"), str(value or ""), retain=False)

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

    start_debug_server(state, host="0.0.0.0", port=8080, command_fn=engine.handle_ui_command)

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
