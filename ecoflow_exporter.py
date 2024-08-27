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
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from prometheus_client import REGISTRY, Gauge, start_http_server

load_dotenv()

DIVISORS = {
    'ecoflow_ac_set_watts': 10,
    'ecoflow_anti_back_flow_flag': 10,
    'ecoflow_bat_error_inv_load_limit': 10,
    'ecoflow_bat_input_volt': 10,
    'ecoflow_bat_input_watts': 10,
    'ecoflow_bat_op_volt': 10,
    'ecoflow_bat_output_load_limit': 10,
    'ecoflow_bat_temp': 10,
    'ecoflow_dynamic_watts': 10,
    'ecoflow_fload_limit_out': 10,
    'ecoflow_gene_watt': 10,
    'ecoflow_grid_cons_watts': 10,
    'ecoflow_inv_brightness': 10,
    'ecoflow_inv_demand_watts': 10,
    'ecoflow_inv_freq': 10,
    'ecoflow_inv_input_volt': 10,
    'ecoflow_inv_op_volt': 10,
    'ecoflow_inv_output_load_limit': 10,
    'ecoflow_inv_output_watts': 10,
    'ecoflow_inv_temp': 10,
    'ecoflow_inv_to_other_watts': 10,
    'ecoflow_inv_to_plug_watts': 10,
    'ecoflow_llc_input_volt': 10,
    'ecoflow_llc_op_volt': 100,
    'ecoflow_llc_temp': 10,
    'ecoflow_permanent_watts': 10,
    'ecoflow_plug_total_watts': 10,
    'ecoflow_pv_power_limit_ac_power': 10,
    'ecoflow_pv_to_inv_watts': 10,
    'ecoflow_pv1_input_cur': 10,
    'ecoflow_pv1_input_volt': 10,
    'ecoflow_pv1_input_watts': 10,
    'ecoflow_pv1_op_volt': 100,
    'ecoflow_pv1_temp': 10,
    'ecoflow_pv2_input_cur': 10,
    'ecoflow_pv2_input_volt': 10,
    'ecoflow_pv2_input_watts': 10,
    'ecoflow_pv2_op_volt': 100,
    'ecoflow_pv2_temp': 10,
    'ecoflow_rated_power': 10,
    'ecoflow_space_demand_watts': 10
}


class EcoflowApiException(Exception):
    pass


class EcoflowMetricException(Exception):
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

    def _generate_auth_params(self) -> Tuple[str, str, str]:
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


class EcoflowMetric:
    def __init__(self, ecoflow_payload_key: str, device_name: str, documentation: str = None):
        self.ecoflow_payload_key = ecoflow_payload_key
        self.device_name = device_name
        self.name = f"ecoflow_{self._convert_key_to_prometheus_name()}"
        self.metric = Gauge(self.name, documentation or f"value from API object key {ecoflow_payload_key}", labelnames=["device"])
        self.value = None
        self.last_update_time = None

    def _convert_key_to_prometheus_name(self) -> str:
        # Convert the payload key to Prometheus format by converting camel case to snake case
        key = re.sub(r'(?<!^)(?=[A-Z])', '_', self.ecoflow_payload_key.split('.')[1].replace('.', '_').replace('Statue', 'Status')).lower()

        # Check if the converted key complies with the Prometheus data model
        if not re.match(r"[a-zA-Z_:][a-zA-Z0-9_:]*", key):
            raise EcoflowMetricException(f"Cannot convert payload key {self.ecoflow_payload_key} to comply with the Prometheus data model. Please, raise an issue!")

        return key

    def set(self, value):
        if self.name in DIVISORS:
            value /= DIVISORS[self.name]

        log.debug(f"Set {self.name} = {value}")

        if self.value != value:
            self.metric.labels(device=self.device_name).set(value)
            self.value = value
            self.last_update_time = time.time()

    def clear(self):
        log.debug(f"Clear {self.name}")
        self.metric.clear()
        self.last_update_time = time.time()


class Worker:
    def __init__(self, ecoflow_api: Any, device_name: str, collecting_interval_seconds: int = 30, expiration_threshold: int = 300):
        self.ecoflow_api = ecoflow_api
        self.device_name = device_name
        self.collecting_interval_seconds = collecting_interval_seconds
        self.metrics_collector: List[EcoflowMetric] = []
        self.expiration_threshold = expiration_threshold
        self.running = True

    def loop(self):
        while self.running:
            try:
                self.process_payload(self.ecoflow_api.get_quota())
            except Exception as error:
                log.error(f"Error processing payload: {error}")
            self.clear_expired_metrics()
            time.sleep(self.collecting_interval_seconds)

    def stop(self):
        self.running = False

    def clear_expired_metrics(self):
        # Clear metrics that haven't been updated for more than the expiration threshold in seconds
        current_time = time.time()
        for metric in self.metrics_collector:
            if current_time - metric.last_update_time > self.expiration_threshold:
                metric.clear()
                log.info(f"Cleared expired metric {metric.name}")

    def create_new_metric(self, ecoflow_payload_key: str) -> EcoflowMetric:
        try:
            metric = EcoflowMetric(ecoflow_payload_key, self.device_name)
            log.info(f"Created new metric from payload key {metric.ecoflow_payload_key} -> {metric.name}")
            return metric
        except EcoflowMetricException as error:
            log.error(error)
            return None

    def get_metric_by_ecoflow_payload_key(self, ecoflow_payload_key: str) -> EcoflowMetric:
        # Find the metric linked to the provided ecoflow payload key, or create a new one if not found
        metric = next((metric for metric in self.metrics_collector if metric.ecoflow_payload_key == ecoflow_payload_key), None)
        if metric:
            log.debug(f"Found metric {metric.name} linked to {ecoflow_payload_key}")
        else:
            log.debug(f"Cannot find metric linked to {ecoflow_payload_key}. Creating new metric")
            metric = self.create_new_metric(ecoflow_payload_key)
            self.metrics_collector.append(metric)
        return metric

    def process_payload(self, params: Dict[str, Any]):
        log.debug(f"Processing params: {params}")
        for ecoflow_payload_key in params.keys():
            # Skip non-status entries (e.g., '20_134.' for statistics)
            if not ecoflow_payload_key.startswith('20_1.'):
                continue

            # Skip unsupported metric types (e.g., non-numeric values)
            ecoflow_payload_value = params[ecoflow_payload_key]
            if not isinstance(ecoflow_payload_value, (int, float)):
                log.info(f"Skipping unsupported metric {ecoflow_payload_key}: {ecoflow_payload_value}")
                continue

            # Create or get the metric for the current payload key
            metric = self.get_metric_by_ecoflow_payload_key(ecoflow_payload_key)
            if metric:
                metric.set(ecoflow_payload_value)


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
    # Register the signal handler for SIGTERM
    signal.signal(signal.SIGTERM, signal_handler)

    # Disable Process and Platform collectors
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
    expiration_threshold = int(os.getenv("EXPIRATION_THRESHOLD", "300"))

    ecoflow_api = EcoflowApi(ecoflow_api_endpoint, ecoflow_accesskey, ecoflow_secretkey, device_sn)
    metrics = Worker(ecoflow_api, device_name, collecting_interval_seconds, expiration_threshold)

    start_http_server(exporter_port)

    try:
        metrics.loop()
    except KeyboardInterrupt:
        log.info("Received KeyboardInterrupt. Exiting...")
        metrics.stop()
        sys.exit(0)


if __name__ == '__main__':
    main()
