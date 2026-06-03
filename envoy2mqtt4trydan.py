#!/usr/bin/env python3
"""
Reads power data from an Enphase Envoy S-Metered gateway and publishes
to the three MQTT topics expected by the V2C Trydan EV charger.

Published topics:
  trydan_v2c_sun_power     — PV production in W (always positive)
  trydan_v2c_grid_power    — grid import/export in W (positive = import, negative = export)
  trydan_v2c_battery_power — battery power in W (0 if no battery)

Two polling modes (set BATTERY_INSTALLED=true to switch):

  Without battery (default):
    GET /ivp/meters/readings — activePower in W, looked up by eid
    Meters discovered once at startup via /ivp/meters.

  With battery:
    GET /ivp/livedata/status — agg_p_mw in mW, divided by 1000
    Stream must be activated first via POST /ivp/livedata/stream {"enable": 1}.
    Provides battery power under meters.storage.agg_p_mw.

Authentication: JWT token from Enphase Enlighten (firmware 7+)
"""

import json
import os
import signal
import sys
import time
import xml.etree.ElementTree as ET

import paho.mqtt.client as mqtt
import requests
import urllib3

urllib3.disable_warnings()

# ---------------------------------------------------------------------------
# Configuration — all settings overridable via environment variables
# ---------------------------------------------------------------------------
ENVOY_HOST     = os.getenv("ENVOY_HOST",     "envoy.local")
ENVOY_USER     = os.getenv("ENVOY_USER",     "")   # Enphase Enlighten account email
ENVOY_PASSWORD = os.getenv("ENVOY_PASSWORD", "")   # Enphase Enlighten password

MQTT_HOST     = os.getenv("MQTT_HOST",      "localhost")
MQTT_PORT     = int(os.getenv("MQTT_PORT",  "1883"))
MQTT_USER     = os.getenv("MQTT_USER",      "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD",  "")
MQTT_USE_TLS  = os.getenv("MQTT_USE_TLS",   "false").lower() == "true"
MQTT_CA_CERT  = os.getenv("MQTT_CA_CERT",   "")    # path to CA file; empty = system CA
MQTT_INSECURE = os.getenv("MQTT_INSECURE",  "false").lower() == "true"  # skip cert verification

TOPIC_SUN     = os.getenv("TOPIC_SUN",     "trydan/trydan_v2c_sun_power")
TOPIC_GRID    = os.getenv("TOPIC_GRID",    "trydan/trydan_v2c_grid_power")
TOPIC_BATTERY = os.getenv("TOPIC_BATTERY", "trydan/trydan_v2c_battery_power")

BATTERY_INSTALLED = os.getenv("BATTERY_INSTALLED", "false").lower() == "true"

POLL_INTERVAL   = float(os.getenv("POLL_INTERVAL",   "0.6"))  # seconds between polls
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY",   "5"))    # seconds between reconnect attempts
DEBUG           = os.getenv("DEBUG", "false").lower() == "true"


def dbg(msg):
    if DEBUG:
        print(f"[debug] {msg}")


# ---------------------------------------------------------------------------
# Envoy serial number retrieval
# ---------------------------------------------------------------------------
def get_envoy_serial():
    url = f"https://{ENVOY_HOST}/info"
    dbg(f"GET {url}")
    resp = requests.get(url, verify=False, timeout=10)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    serial = next((e.text for e in root.iter("sn")), None)
    if not serial:
        sys.exit("[error] Could not read serial number from /info")
    return serial


# ---------------------------------------------------------------------------
# JWT authentication via Enphase Enlighten
# ---------------------------------------------------------------------------
def fetch_token(serial):
    dbg(f"POST https://enlighten.enphaseenergy.com/login/login.json? (user={ENVOY_USER})")
    data = {"user[email]": ENVOY_USER, "user[password]": ENVOY_PASSWORD}
    r1 = requests.post("https://enlighten.enphaseenergy.com/login/login.json?", data=data, timeout=15)
    r1.raise_for_status()
    session_id = r1.json()["session_id"]
    dbg(f"session_id: {session_id}")
    r2 = requests.post(
        "https://entrez.enphaseenergy.com/tokens",
        json={"session_id": session_id, "serial_num": serial, "username": ENVOY_USER},
        timeout=15,
    )
    r2.raise_for_status()
    token = r2.text.strip()
    dbg(f"token received: {token[:30]}…")
    print("[auth] New token generated")
    return token


# ---------------------------------------------------------------------------
# Meter discovery — maps measurementType to eid via /ivp/meters
# ---------------------------------------------------------------------------
def get_meter_eids(headers):
    """Return {measurementType: eid} for all enabled meters."""
    url = f"https://{ENVOY_HOST}/ivp/meters"
    dbg(f"GET {url}")
    resp = requests.get(url, headers=headers, verify=False, timeout=10)
    if resp.status_code == 401:
        raise requests.exceptions.HTTPError(response=resp)
    resp.raise_for_status()
    meters = resp.json()
    dbg(f"meters: {json.dumps(meters)}")
    return {m["measurementType"]: m["eid"] for m in meters if m.get("state") == "enabled"}


# ---------------------------------------------------------------------------
# MQTT client
# ---------------------------------------------------------------------------
_mqtt_connected    = False
_app_ready         = False
_last_publish_time = 0.0


def make_mqtt_client():
    global _mqtt_connected

    if MQTT_PORT == 8883 and not MQTT_USE_TLS:
        print("[warning] Port 8883 detected but MQTT_USE_TLS=false — set MQTT_USE_TLS=true for TLS brokers")

    def on_connect(c, u, f, rc, p):
        global _mqtt_connected
        _mqtt_connected = (rc.value == 0)
        print(f"[mqtt] Connected (code {rc})")

    def on_disconnect(c, u, f, rc, p):
        global _mqtt_connected
        _mqtt_connected = False
        print(f"[mqtt] Disconnected (code {rc})")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="envoy2mqtt4trydan")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    if MQTT_USE_TLS:
        import ssl
        ca = MQTT_CA_CERT if MQTT_CA_CERT else None
        dbg(f"TLS enabled — CA={ca or 'system'}, insecure={MQTT_INSECURE}")
        client.tls_set(ca_certs=ca, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        if MQTT_INSECURE:
            client.tls_insecure_set(True)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    dbg(f"Connecting to MQTT broker {MQTT_HOST}:{MQTT_PORT} (tls={MQTT_USE_TLS}, user={MQTT_USER or 'anonymous'})")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


# ---------------------------------------------------------------------------
# Envoy polling — without battery: /ivp/meters/readings
# ---------------------------------------------------------------------------
def poll_meters(headers, meter_eids):
    url = f"https://{ENVOY_HOST}/ivp/meters/readings"
    dbg(f"GET {url}")
    resp = requests.get(url, headers=headers, verify=False, timeout=5)
    if resp.status_code == 401:
        raise requests.exceptions.HTTPError(response=resp)
    resp.raise_for_status()
    readings = resp.json()
    dbg(f"response: {json.dumps(readings)}")

    by_eid = {r["eid"]: r for r in readings}
    production_eid      = meter_eids.get("production")
    net_consumption_eid = meter_eids.get("net-consumption")

    sun_power     = round(by_eid[production_eid]["activePower"])      if production_eid      in by_eid else 0
    grid_power    = round(by_eid[net_consumption_eid]["activePower"]) if net_consumption_eid in by_eid else 0
    battery_power = 0
    return sun_power, grid_power, battery_power


# ---------------------------------------------------------------------------
# Envoy polling — with battery: /ivp/livedata/status
# ---------------------------------------------------------------------------
def ensure_livedata_stream(headers):
    """Activate the livedata stream if not already running."""
    url_status = f"https://{ENVOY_HOST}/ivp/livedata/status"
    dbg(f"GET {url_status}")
    resp = requests.get(url_status, headers=headers, verify=False, timeout=5)
    if resp.status_code == 401:
        raise requests.exceptions.HTTPError(response=resp)
    resp.raise_for_status()
    if resp.json().get("connection", {}).get("sc_stream") != "enabled":
        print("[envoy] Activating livedata stream…")
        url_activate = f"https://{ENVOY_HOST}/ivp/livedata/stream"
        r = requests.post(url_activate, headers=headers, verify=False, timeout=5, json={"enable": 1})
        if r.status_code == 401:
            raise requests.exceptions.HTTPError(response=r)
        r.raise_for_status()
        if r.json().get("sc_stream") != "enabled":
            raise RuntimeError(f"Failed to activate livedata stream: {r.text}")
        print("[envoy] Livedata stream activated")


def poll_livedata(headers):
    url = f"https://{ENVOY_HOST}/ivp/livedata/status"
    dbg(f"GET {url}")
    resp = requests.get(url, headers=headers, verify=False, timeout=5)
    if resp.status_code == 401:
        raise requests.exceptions.HTTPError(response=resp)
    resp.raise_for_status()
    payload = resp.json()
    dbg(f"response: {json.dumps(payload)}")

    meters = payload.get("meters", {})
    sun_power     = round(meters.get("pv",      {}).get("agg_p_mw", 0.0) / 1000)
    grid_power    = round(meters.get("grid",    {}).get("agg_p_mw", 0.0) / 1000)
    battery_power = round(meters.get("storage", {}).get("agg_p_mw", 0.0) / 1000)
    return sun_power, grid_power, battery_power


# ---------------------------------------------------------------------------
# MQTT publishing
# ---------------------------------------------------------------------------
def publish(client, sun_power, grid_power, battery_power):
    global _last_publish_time
    if not _mqtt_connected:
        raise ConnectionError("MQTT broker not connected")

    client.publish(TOPIC_SUN,     str(sun_power),     qos=0)
    client.publish(TOPIC_GRID,    str(grid_power),    qos=0)
    client.publish(TOPIC_BATTERY, str(battery_power), qos=0)
    _last_publish_time = time.monotonic()

    print(
        f"[publish] {TOPIC_SUN}={sun_power}W  "
        f"{TOPIC_GRID}={grid_power}W  "
        f"{TOPIC_BATTERY}={battery_power}W"
    )


# ---------------------------------------------------------------------------
# Embedded HTTP health server (Kubernetes readiness + liveness probes)
# ---------------------------------------------------------------------------
import http.server
import socketserver
import threading

_HEALTH_PORT      = 8080
_LIVENESS_TIMEOUT = max(60.0, POLL_INTERVAL * 10)


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/readyz":
            if _app_ready and _mqtt_connected:
                self._respond(200, "OK")
            else:
                self._respond(503, "NOT READY")
        elif self.path == "/healthz":
            if not _app_ready:
                self._respond(200, "STARTING")
            elif time.monotonic() - _last_publish_time < _LIVENESS_TIMEOUT:
                self._respond(200, "OK")
            else:
                self._respond(503, "STALE")
        else:
            self._respond(404, "NOT FOUND")

    def _respond(self, code, body):
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


def start_health_server():
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("0.0.0.0", _HEALTH_PORT), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    t.start()
    print(f"[health] Listening on 0.0.0.0:{_HEALTH_PORT} (/healthz, /readyz)")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    start_health_server()
    if not ENVOY_USER or not ENVOY_PASSWORD:
        sys.exit("[error] ENVOY_USER and ENVOY_PASSWORD are required. Set the environment variables.")

    print("[init] Reading Envoy serial number…")
    serial = get_envoy_serial()
    print(f"[init] Serial={serial}")

    token = fetch_token(serial)
    headers = {"Authorization": f"Bearer {token}"}

    if BATTERY_INSTALLED:
        print("[envoy] Mode: livedata (battery installed)")
        ensure_livedata_stream(headers)
        meter_eids = None
    else:
        print("[envoy] Mode: meters/readings (no battery)")
        meter_eids = get_meter_eids(headers)
        print(f"[envoy] Meters: {meter_eids}")
        if "production" not in meter_eids:
            sys.exit("[error] No enabled production meter found on this Envoy")
        if "net-consumption" not in meter_eids:
            sys.exit("[error] No enabled net-consumption meter found on this Envoy")

    print("[mqtt] Connecting to broker…")
    client = make_mqtt_client()
    time.sleep(1)  # wait for MQTT connection to establish

    if not _mqtt_connected:
        sys.exit(f"[error] Could not connect to MQTT broker {MQTT_HOST}:{MQTT_PORT}")

    global _app_ready
    _app_ready = True
    print("[health] Application ready")

    def shutdown(signum, frame):
        print("[shutdown] Publishing zero values before exit…")
        client.publish(TOPIC_SUN,     "0", qos=0)
        client.publish(TOPIC_GRID,    "0", qos=0)
        client.publish(TOPIC_BATTERY, "0", qos=0)
        time.sleep(0.5)  # let paho flush the outgoing queue
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    while True:
        try:
            if BATTERY_INSTALLED:
                sun_power, grid_power, battery_power = poll_livedata(headers)
            else:
                sun_power, grid_power, battery_power = poll_meters(headers, meter_eids)
            publish(client, sun_power, grid_power, battery_power)
            time.sleep(POLL_INTERVAL)
        except ConnectionError as e:
            print(f"[error] {e} — retrying in {RECONNECT_DELAY}s")
            time.sleep(RECONNECT_DELAY)
        except RuntimeError as e:
            print(f"[error] {e} — retrying in {RECONNECT_DELAY}s")
            time.sleep(RECONNECT_DELAY)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                print("[auth] Token expired, refreshing…")
                token = fetch_token(serial)
                headers = {"Authorization": f"Bearer {token}"}
                if BATTERY_INSTALLED:
                    ensure_livedata_stream(headers)
                else:
                    meter_eids = get_meter_eids(headers)
            else:
                print(f"[error] HTTP {e} — retrying in {RECONNECT_DELAY}s")
                time.sleep(RECONNECT_DELAY)
        except requests.exceptions.RequestException as e:
            print(f"[error] {e} — retrying in {RECONNECT_DELAY}s")
            time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()
