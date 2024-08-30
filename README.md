# ⚡ EcoFlow to Prometheus Exporter (Public API Version)

## About

This project is a fork of the original [EcoFlow Exporter](https://github.com/berezhinskiy/ecoflow_exporter) by [berezhinskiy](https://github.com/berezhinskiy). The main difference is that this version uses the public API of EcoFlow with access key and secret instead of the original method that used username and password (pretending to be a mobile app client).

The project provides:

- [Python program](ecoflow_exporter.py) that accepts arguments to collect information about a device and exports the collected metrics to a Prometheus endpoint
- [Dashboard for Grafana](https://grafana.com/grafana/dashboards/17812-ecoflow/)
- [Docker image](https://github.com/dmitry-semenov/ecoflow_exporter/pkgs/container/ecoflow_exporter) for your convenience
- [Quick Start guide](docker-compose/) for your pleasure

## Disclaimers

⚠️ This project is in no way connected to EcoFlow company, and is entirely developed as a fun project with no guarantees of anything.

⚠️ This has only been tested with the following EcoFlow products:

- **River Pro**
- **DELTA Pro**

Please create an issue to let us know if the exporter works well (or not) with your model.

## Usage

- Connect the device to WiFi and register an EcoFlow account using the official mobile application
- Get your unit's serial number
- Obtain your EcoFlow API access key and secret key
- Exporter is parameterized via environment variables:

Required:

`DEVICE_SN` - the device serial number

`ECOFLOW_ACCESS_KEY` - your EcoFlow API access key

`ECOFLOW_SECRET_KEY` - your EcoFlow API secret key

Optional:

`CLIENT_ID` - If provided, we'll use a static client ID for the MQTT connection. Note that only 10 unique client IDs are allowed per day. If you frequently encounter "Not authorized" errors, try recreating your access key and secret and set a static client ID.

`DEVICE_NAME` - If given, this name will be exported as `device` label instead of the device serial number

`EXPORTER_PORT` - (default: `9090`)

`LOG_LEVEL` - (default: `INFO`) Possible values: `DEBUG`, `INFO`, `WARNING`, `ERROR`

`ECOFLOW_API_HOST` - (default: `api-e.ecoflow.com`) API host to use

- Example of running docker image:

```bash
docker run -e DEVICE_SN=<your device SN> -e ECOFLOW_ACCESS_KEY=<your access_key> -e ECOFLOW_SECRET_KEY=<your secret> -it -p 9090:9090 --network=host ghcr.io/dmitry-semenov/ecoflow_exporter
```

This will run the image with the exporter on `*:9090`

## Quick Start

Don't know anything about Prometheus? Want a quick start? Lazy person? [This guide](docker-compose/) is for you.

## Metrics

[List of metrics should match the original project](https://github.com/berezhinskiy/ecoflow_exporter?tab=readme-ov-file#metrics)

## Acknowledgments

This project forks the original [EcoFlow Exporter](https://github.com/berezhinskiy/ecoflow_exporter) by [berezhinskiy](https://github.com/berezhinskiy) via [EcoFlow Exporter](https://github.com/michikrug/ecoflow_exporter) by [michikrug](https://github.com/michikrug). Michikrug began exploring the public API approach, but uses API polling instead of MQTT.

## License

This project is licensed under the GNU General Public License. See the [LICENSE](LICENSE) file for details.
