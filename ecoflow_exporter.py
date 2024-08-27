import base64
import hashlib
import hmac
import json
import logging as log
import os
import re
import signal
import sys
import time
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from prometheus_client import REGISTRY, Gauge, Counter, start_http_server

load_dotenv()

class EcoflowApiException(Exception):
    pass

class EcoflowApi:
    def __init__(self, api_endpoint: str, accesskey: str, secretkey: str, device_sn: str):
        self.api_endpoint = api_endpoint
        self.accesskey = accesskey
        self.secretkey = secretkey
        self.device_sn = device_sn

    def get_quota(self) -> Dict[str, Any]:
        timestamp, nonce, sign = self._generate_auth_params()
        url = f"https://{self.api_endpoint}/iot-open/sign/device/quota/all?sn={self.device_sn}"
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'accessKey': self.accesskey,
            'nonce': nonce,
            'timestamp': timestamp,
            'sign': sign,
        }

        log.info(f"Getting payload from {url}")

        response = requests.get(url, headers=headers)
        return self._get_json_response(response)

    def _generate_auth_params(self):
        timestamp = str(int(time.time() * 1000))
        nonce = base64.b64encode(hashlib.sha256(timestamp.encode()).digest()).decode()[:-1]
        to_sign = f'accessKey={self.accesskey}&nonce={nonce}&timestamp={timestamp}'
        sign = hmac.new(self.secretkey.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        return timestamp, nonce, sign

    def _get_json_response(self, response: requests.Response) -> Dict[str, Any]:
        if response.status_code != 200:
            raise EcoflowApiException(f"HTTP status code {response.status_code}: {response.text}")

        try:
            response_data = response.json()
            if response_data.get("message", "").lower() != "success":
                raise EcoflowApiException(f"API response error: {response_data.get('message', 'Unknown error')}")
            return response_data["data"]
        except KeyError as key:
            raise EcoflowApiException(f"Missing key {key} in response: {response.text}")
        except json.JSONDecodeError as error:
            raise EcoflowApiException(f"Failed to parse JSON response: {response.text}, Error: {error}")

class Worker:
    def __init__(self, ecoflow_api: EcoflowApi, device_name: str, collecting_interval_seconds: int = 30):
        self.ecoflow_api = ecoflow_api
        self.device_name = device_name
        self.collecting_interval_seconds = collecting_interval_seconds
        self.metrics: Dict[str, Gauge] = {}
        self.running = True
        self.api_requests_total = Counter('ecoflow_api_requests_total', 'Total number of API requests', ['device'])
        self.api_errors_total = Counter('ecoflow_api_errors_total', 'Total number of API errors', ['device'])
        self.online_metric = Gauge('ecoflow_online', 'Device online status', ['device'])

    def loop(self):
        while self.running:
            try:
                self.api_requests_total.labels(device=self.device_name).inc()
                payload = self.ecoflow_api.get_quota()
                log.debug(f"Received payload: {payload}")
                self.process_payload(payload)
                self.online_metric.labels(device=self.device_name).set(1)
            except Exception as error:
                self.api_errors_total.labels(device=self.device_name).inc()
                self.online_metric.labels(device=self.device_name).set(0)
                log.error(f"Error processing payload: {error}")
            time.sleep(self.collecting_interval_seconds)

    def stop(self):
        self.running = False

    def get_or_create_metric(self, metric_name: str) -> Gauge:
        if metric_name not in self.metrics:
            self.metrics[metric_name] = Gauge(metric_name, f"Value for {metric_name}", ['device'])
        return self.metrics[metric_name]

    def process_payload(self, params: Dict[str, Any]):
        log.debug(f"Processing params: {params}")
        for key, value in params.items():
            if not isinstance(value, (int, float)):
                log.debug(f"Skipping non-numeric metric {key}: {value}")
                continue

            metric_name = f"ecoflow_{key.lower().replace('.', '_')}"
            metric = self.get_or_create_metric(metric_name)
            metric.labels(device=self.device_name).set(value)
            log.debug(f"Updated metric {metric_name} with value {value}")

def signal_handler(signum: int, frame: Optional[object]) -> None:
    log.info(f"Received signal {signum}. Exiting...")
    sys.exit(0)

def load_env_variable(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        log.error(f"Environment variable {name} is required.")
        sys.exit(1)
    return value

def main() -> None:
    signal.signal(signal.SIGTERM, signal_handler)

    for collector in list(REGISTRY._collector_to_names.keys()):
        REGISTRY.unregister(collector)

    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(log, log_level_str, log.INFO)
    log.basicConfig(stream=sys.stdout, level=log_level, format='%(asctime)s %(levelname)-7s %(message)s')

    device_sn = load_env_variable("DEVICE_SN")
    device_name = os.getenv("DEVICE_NAME", device_sn)
    ecoflow_accesskey = load_env_variable("ECOFLOW_ACCESSKEY")
    ecoflow_secretkey = load_env_variable("ECOFLOW_SECRETKEY")
    ecoflow_api_endpoint = os.getenv("ECOFLOW_API_ENDPOINT", "api-e.ecoflow.com")
    exporter_port = int(os.getenv("EXPORTER_PORT", "9090"))
    collecting_interval_seconds = int(os.getenv("COLLECTING_INTERVAL", "30"))

    log.info(f"Starting Ecoflow exporter for device: {device_name} (SN: {device_sn})")
    log.info(f"API endpoint: {ecoflow_api_endpoint}")
    log.info(f"Exporter port: {exporter_port}")
    log.info(f"Collecting interval: {collecting_interval_seconds} seconds")

    ecoflow_api = EcoflowApi(ecoflow_api_endpoint, ecoflow_accesskey, ecoflow_secretkey, device_sn)
    metrics = Worker(ecoflow_api, device_name, collecting_interval_seconds)

    start_http_server(exporter_port)
    log.info(f"HTTP server started on port {exporter_port}")

    try:
        metrics.loop()
    except KeyboardInterrupt:
        log.info("Received KeyboardInterrupt. Exiting...")
        metrics.stop()
        sys.exit(0)

if __name__ == '__main__':
    main()
