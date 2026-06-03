# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Single-file Python script that bridges an Enphase Envoy S-Metered inverter gateway to a V2C Trydan EV charger via MQTT. The Trydan subscribes natively to three topics to drive its dynamic charging mode.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # then edit .env
export $(grep -v '^#' .env | xargs)
python3 envoy2mqtt4trydan.py
```

`ENVOY_USER` and `ENVOY_PASSWORD` (Enphase Enlighten account) are mandatory — the script exits immediately if absent.

## Architecture

The entire logic lives in `envoy2mqtt4trydan.py`. Execution flow:

1. **Serial retrieval** — `GET https://<ENVOY_HOST>/info` (XML) to extract the gateway serial number
2. **Token auth** — JWT fetched from `enlighten.enphaseenergy.com` then `entrez.enphaseenergy.com`; held in memory, renewed on HTTP 401
3. **Mode selection** — driven by `BATTERY_INSTALLED` env var:
   - `false` (default) → `poll_meters()`: discovers meter eids via `GET /ivp/meters`, then polls `GET /ivp/meters/readings` and reads `activePower` by eid
   - `true` → `poll_livedata()`: activates stream via `POST /ivp/livedata/stream`, then polls `GET /ivp/livedata/status` and reads `meters.pv/grid/storage.agg_p_mw / 1000`
4. **MQTT publish** — `publish()` called after each poll, three topics as plain integer strings (watts)

## MQTT topics

| Variable | Default topic | Semantics |
|---|---|---|
| `TOPIC_SUN` | `trydan/trydan_v2c_sun_power` | PV production, always >= 0 |
| `TOPIC_GRID` | `trydan/trydan_v2c_grid_power` | Grid import/export (+= import, -= export) |
| `TOPIC_BATTERY` | `trydan/trydan_v2c_battery_power` | Battery power (always 0, no battery meter on Envoy S-Metered) |

## Configuration — all via environment variables

| Variable | Default | Notes |
|---|---|---|
| `ENVOY_HOST` | `envoy.local` | IP or hostname of the Envoy |
| `ENVOY_USER` | — | Enphase Enlighten email (**required**) |
| `ENVOY_PASSWORD` | — | Enphase Enlighten password (**required**) |
| `BATTERY_INSTALLED` | `false` | `true` → use `/ivp/livedata/status`; `false` → use `/ivp/meters/readings` |
| `MQTT_HOST` | `localhost` | |
| `MQTT_PORT` | `1883` | Use `8883` with TLS |
| `MQTT_USER` / `MQTT_PASSWORD` | — | Leave empty for anonymous |
| `MQTT_USE_TLS` | `false` | Enable SSL/TLS |
| `MQTT_CA_CERT` | — | Path to CA file; empty = system CA |
| `MQTT_INSECURE` | `false` | Skip cert verification (self-signed brokers) |
| `RECONNECT_DELAY` | `5` | Seconds between reconnect attempts |
| `DEBUG` | `false` | Logs HTTP requests, full JSON responses, auth/MQTT details |
