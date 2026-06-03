# envoy2mqtt4trydan

Bridges an **Enphase Envoy S-Metered** gateway to a **V2C Trydan** EV charger via MQTT.

The script polls the Envoy for power readings and publishes three values to the MQTT topics that the Trydan subscribes to natively for its dynamic charging mode.

```
Enphase Envoy S-Metered  ──→  envoy2mqtt4trydan.py  ──→  MQTT  ──→  V2C Trydan
```

## Requirements

- Python 3.8+
- Enphase Envoy S-Metered with firmware 7+ (D7.x or D8.x)
- An [Enphase Enlighten](https://enlighten.enphaseenergy.com) account
- An MQTT broker reachable by both this script and the Trydan

## Installation

```bash
git clone https://github.com/jeanfabrice/envoy2mqtt4trydan.git
cd envoy2mqtt4trydan
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your values, then:

```bash
export $(grep -v '^#' .env | xargs)
python3 envoy2mqtt4trydan.py
```

## Configuration

All settings are provided via environment variables (or a `.env` file).

| Variable | Default | Description |
|---|---|---|
| `ENVOY_HOST` | `envoy.local` | IP address or hostname of the Envoy |
| `ENVOY_USER` | — | Enphase Enlighten email (**required**) |
| `ENVOY_PASSWORD` | — | Enphase Enlighten password (**required**) |
| `BATTERY_INSTALLED` | `false` | `true` → poll `/ivp/livedata/status` (includes battery); `false` → poll `/ivp/meters/readings` |
| `MQTT_HOST` | `localhost` | MQTT broker hostname or IP |
| `MQTT_PORT` | `1883` | MQTT broker port (`8883` with TLS) |
| `MQTT_USER` | — | MQTT username (leave empty for anonymous) |
| `MQTT_PASSWORD` | — | MQTT password |
| `MQTT_USE_TLS` | `false` | Enable SSL/TLS for the MQTT connection |
| `MQTT_CA_CERT` | — | Path to a CA certificate file; empty = system CA |
| `MQTT_INSECURE` | `false` | Skip TLS certificate verification (self-signed brokers) |
| `TOPIC_SUN` | `trydan/trydan_v2c_sun_power` | Topic for PV production |
| `TOPIC_GRID` | `trydan/trydan_v2c_grid_power` | Topic for grid import/export |
| `TOPIC_BATTERY` | `trydan/trydan_v2c_battery_power` | Topic for battery power |
| `POLL_INTERVAL` | `0.6` | Seconds between polling requests (only when `BATTERY_INSTALLED=false`) |
| `RECONNECT_DELAY` | `5` | Seconds to wait before reconnecting after an error |
| `DEBUG` | `false` | Print HTTP requests, full JSON responses, and auth/MQTT details |

## MQTT payload format

Each topic receives a plain integer string representing watts, published at the Envoy stream rate (~1 Hz):

```
trydan/trydan_v2c_sun_power     → "3450"
trydan/trydan_v2c_grid_power    → "-820"   # negative = exporting to grid
trydan/trydan_v2c_battery_power → "0"
```

## Health probes (Kubernetes)

The container exposes an HTTP server on port **8080** with two endpoints:

| Endpoint | Probe | Behaviour |
|---|---|---|
| `/healthz` | liveness | `200` while the process is alive; `503` if the main loop stops publishing (stale) |
| `/readyz` | readiness | `200` once initialised and MQTT is connected; `503` otherwise |

Kubernetes probe configuration:

```yaml
readinessProbe:
  httpGet:
    path: /readyz
    port: 8080
  initialDelaySeconds: 30
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 3

livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 90
  periodSeconds: 30
  timeoutSeconds: 5
  failureThreshold: 3
```

## Running with Docker

```bash
docker build -t envoy2mqtt4trydan .
docker run --env-file .env envoy2mqtt4trydan
```

Or with explicit variables:

```bash
docker run \
  -e ENVOY_HOST=192.168.1.x \
  -e ENVOY_USER=your@email.com \
  -e ENVOY_PASSWORD=yourpassword \
  -e MQTT_HOST=192.168.1.y \
  envoy2mqtt4trydan
```

## Running as a systemd service

```ini
[Unit]
Description=Envoy to MQTT bridge for V2C Trydan
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/envoy2mqtt4trydan.env
ExecStart=/usr/bin/python3 /opt/envoy2mqtt4trydan/envoy2mqtt4trydan.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## License

[MIT](LICENSE)
