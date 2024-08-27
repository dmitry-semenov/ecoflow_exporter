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
from prometheus_client import REGISTRY, Gauge, Counter, start_http_server

load_dotenv()

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
        self.name = self._convert_key_to_prometheus_name()
        self.metric = Gauge(self.name, documentation or f"value from API object key {ecoflow_payload_key}", labelnames=["device"])
        self.value = None
        self.last_update_time = None

    def _convert_key_to_prometheus_name(self) -> str:
        # Special cases
        special_cases = {
            "pd.wattsOutSum": "pd_watts_out_sum",
            "inv.acInVol": "inv_ac_in_vol",
            "inv.acInAmp": "inv_ac_in_amp",
            "inv.invOutVol": "inv_inv_out_vol",
            "inv.invOutAmp": "inv_inv_out_amp",
            "inv.acOutFreq": "inv_cfg_ac_out_freq",
            "bms.bmsStatus.vol": "bms_master_vol",
            "bms.bmsStatus.amp": "bms_master_amp",
            "bms.bmsStatus.soc": "bms_master_soc",
            "bms.bmsStatus.remainCap": "bms_master_remain_cap",
            "bms.bmsStatus.fullCap": "bms_master_full_cap",
            "bms.bmsStatus.cycles": "bms_master_cycles",
            "bms.bmsStatus.minCellTemp": "bms_master_min_cell_temp",
            "bms.bmsStatus.maxCellTemp": "bms_master_max_cell_temp",
            "bms.bmsStatus.minCellVol": "bms_master_min_cell_vol",
            "bms.bmsStatus.maxCellVol": "bms_master_max_cell_vol",
            "ems.chgRemainTime": "ems_chg_remain_time",
            "ems.dsgRemainTime": "ems_dsg_remain_time",
        }

        if self.ecoflow_payload_key in special_cases:
            return special_cases[self.ecoflow_payload_key]

        # Remove the '20_1.' prefix and convert to lowercase
        key = self.ecoflow_payload_key.split('.', 1)[-1].lower()
        
        # Convert camelCase to snake_case
        key = re.sub(r'(?<!^)(?=[A-Z])', '_', key)
        
        # Replace any invalid characters with underscores
        key = re.sub(r'[^a-z0-9_]', '_', key)
        
        # Ensure the key starts with a letter or underscore
        if not key[0].isalpha() and key[0] != '_':
            key = f"_{key}"
        
        return key

    def set(self, value):
        log.debug(f"Setting {self.name} = {value}")
        if self.value != value:
            self.metric.labels(device=self.device_name).set(value)
            self.value = value
            self.last_update_time = time.time()

    def clear(self):
        log.debug(f"Clearing {self.name}")
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
        self.api_requests_total = Counter('ecoflow_api_requests_total', 'Total number of API requests', ['device'])
        self.api_errors_total = Counter('ecoflow_api_errors_total', 'Total number of API errors', ['device'])
        self.online_metric = Gauge('ecoflow_online', 'Device online status', ['device'])
        self.mqtt_messages_total = Counter('ecoflow_mqtt_messages_receive_total', 'Total number of MQTT messages received', ['device'])

    def loop(self):
        while self.running:
            try:
                self.api_requests_total.labels(device=self.device_name).inc()
                payload = self.ecoflow_api.get_quota()
                log.debug(f"Received payload: {payload}")
                self.process_payload(payload)
                self.online_metric.labels(device=self.device_name).set(1)
                self.mqtt_messages_total.labels(device=self.device_name).inc()
            except Exception as error:
                self.api_errors_total.labels(device=self.device_name).inc()
                self.online_metric.labels(device=self.device_name).set(0)
                log.error(f"Error processing payload: {error}")
            self.clear_expired_metrics()
            time.sleep(self.collecting_interval_seconds)

    def stop(self):
        self.running = False

    def clear_expired_metrics(self):
        current_time = time.time()
        for metric in self.metrics_collector:
            if metric.last_update_time and current_time - metric.last_update_time > self.expiration_threshold:
                metric.clear()
                log.info(f"Cleared expired metric {metric.name}")

    def create_new_metric(self, ecoflow_payload_key: str) -> Optional[EcoflowMetric]:
        try:
            metric = EcoflowMetric(ecoflow_payload_key, self.device_name)
            log.info(f"Created new metric from payload key {metric.ecoflow_payload_key} -> {metric.name}")
            return metric
        except EcoflowMetricException as error:
            log.error(f"Error creating metric: {error}")
            return None

    def get_metric_by_ecoflow_payload_key(self, ecoflow_payload_key: str) -> Optional[EcoflowMetric]:
        metric = next((metric for metric in self.metrics_collector if metric.ecoflow_payload_key == ecoflow_payload_key), None)
        if metric:
            log.debug(f"Found metric {metric.name} linked to {ecoflow_payload_key}")
        else:
            log.debug(f"Cannot find metric linked to {ecoflow_payload_key}. Creating new metric")
            metric = self.create_new_metric(ecoflow_payload_key)
            if metric:
                self.metrics_collector.append(metric)
        return metric

    def flatten_dict(self, d: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, Any]:
        items = []
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.extend(self.flatten_dict(v, new_key, sep=sep).items())
            else:
                items.append((new_key, v))
        return dict(items)

    def process_payload(self, params: Dict[str, Any]):
        log.debug(f"Processing params: {params}")
        flattened_params = self.flatten_dict(params)
        for ecoflow_payload_key, ecoflow_payload_value in flattened_params.items():
            if not isinstance(ecoflow_payload_value, (int, float)):
                log.debug(f"Skipping non-numeric metric {ecoflow_payload_key}: {ecoflow_payload_value}")
                continue

            metric = self.get_metric_by_ecoflow_payload_key(ecoflow_payload_key)
            if metric:
                metric.set(ecoflow_payload_value)
            else:
                log.warning(f"Failed to process metric for payload key: {ecoflow_payload_key}")

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
    expiration_threshold = int(os.getenv("EXPIRATION_THRESHOLD", "300"))

    log.info(f"Starting Ecoflow exporter for device: {device_name} (SN: {device_sn})")
    log.info(f"API endpoint: {ecoflow_api_endpoint}")
    log.info(f"Exporter port: {exporter_port}")
    log.info(f"Collecting interval: {collecting_interval_seconds} seconds")
    log.info(f"Expiration threshold: {expiration_threshold} seconds")

    ecoflow_api = EcoflowApi(ecoflow_api_endpoint, ecoflow_accesskey, ecoflow_secretkey, device_sn)
    metrics = Worker(ecoflow_api, device_name, collecting_interval_seconds, expiration_threshold)

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
