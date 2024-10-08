import logging as log
import sys
import os
import signal
import ssl
import time
import json
import re
import base64
import hashlib
import hmac
from datetime import datetime
from queue import Queue
from threading import Timer
from multiprocessing import Process
import requests
import paho.mqtt.client as mqtt
from prometheus_client import start_http_server, REGISTRY, Gauge, Counter

DIVISORS = {
    'bmsMaster.amp': 1000,  # Convert mA to A
    'bmsMaster.designCap': 1000,  # Convert mAh to Ah
    'bmsMaster.fullCap': 1000,  # Convert mAh to Ah
    'bmsMaster.maxCellVol': 1000,  # Convert mV to V
    'bmsMaster.minCellVol': 1000,  # Convert mV to V
    'bmsMaster.remainCap': 1000,  # Convert mAh to Ah
    'bmsMaster.tagChgAmp': 1000,  # Convert mA to A
    'bmsMaster.vol': 1000,  # Convert mV to V
    'bmsMaster.inputWatts': 1000,  # Convert W to kW
    'bmsMaster.outputWatts': 1000,  # Convert W to kW
    'ems.chgAmp': 1000,  # Convert mA to A
    'ems.chgVol': 1000,  # Convert mV to V
    'ems.paraVolMax': 1000,  # Convert mV to V
    'ems.paraVolMin': 1000,  # Convert mV to V
    'inv.acInAmp': 1000,  # Convert mA to A
    'inv.acInVol': 1000,  # Convert mV to V
    'inv.cfgAcOutVoltage': 1000,  # Convert mV to V
    'inv.dcInAmp': 1000,  # Convert mA to A
    'inv.dcInVol': 1000,  # Convert mV to V
    'inv.invOutAmp': 1000,  # Convert mA to A
    'inv.invOutVol': 1000,  # Convert mV to V
    'mppt.carOutAmp': 1000,  # Convert mA to A
    'mppt.carOutVol': 1000,  # Convert mV to V
    'mppt.cfgDcChgCurrent': 1000,  # Convert mA to A
    'mppt.dcdc12vAmp': 1000,  # Convert mA to A
    'mppt.dcdc12vVol': 1000,  # Convert mV to V
    'mppt.inAmp': 1000,  # Convert mA to A
    'mppt.inVol': 1000,  # Convert mV to V
    'mppt.outAmp': 1000,  # Convert mA to A
    'mppt.outVol': 1000,  # Convert mV to V
    'pd.chgPowerAc': 1000,  # Convert W to kW
    'pd.dsgPowerAc': 1000,  # Convert W to kW
    'pd.chgPowerDc': 1000,  # Convert Wh to kWh
    'pd.chgSunPower': 1000,  # Convert Wh to kWh
    'pd.dsgPowerDc': 1000,  # Convert Wh to kWh
}

# Time-related conversions
TIME_CONVERSIONS = {
    'pd.remainTime': 60,  # Convert minutes to seconds
    'ems.chgRemainTime': 60,  # Convert minutes to seconds
    'ems.dsgRemainTime': 60,  # Convert minutes to seconds
    'pd.invUsedTime': 1,  # Already in seconds
    'pd.usbUsedTime': 1,  # Already in seconds
    'pd.usbqcUsedTime': 1,  # Already in seconds
    'pd.typccUsedTime': 1,  # Already in seconds
    'pd.carUsedTime': 1,  # Already in seconds
    'pd.dcInUsedTime': 1,  # Already in seconds
    'pd.mpptUsedTime': 1,  # Already in seconds
}

class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)

class EcoflowMetricException(Exception):
    pass


class EcoflowAuthentication:
    def __init__(self, client_id, ecoflow_access_key, ecoflow_secret_key, ecoflow_api_host):
        self.ecoflow_access_key = ecoflow_access_key
        self.ecoflow_secret_key = ecoflow_secret_key
        self.ecoflow_api_host = ecoflow_api_host
        self.mqtt_url = "mqtt.ecoflow.com"
        self.mqtt_port = 8883
        self.mqtt_username = None
        self.mqtt_password = None
        self.mqtt_client_id = client_id
        self.authorize()

    def authorize(self):
        url = f"https://{self.ecoflow_api_host}/iot-open/sign/certification"
        timestamp = str(int(time.time() * 1000))
        nonce = base64.b64encode(hashlib.sha256(timestamp.encode()).digest()).decode()[:-1]
        to_sign = f'accessKey={self.ecoflow_access_key}&nonce={nonce}&timestamp={timestamp}'
        sign = hmac.new(self.ecoflow_secret_key.encode(), to_sign.encode(), hashlib.sha256).hexdigest()

        headers = {
            'accessKey': self.ecoflow_access_key,
            'timestamp': timestamp,
            'nonce': nonce,
            'sign': sign
        }

        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            data = response.json()['data']
            self.mqtt_url = data['url']
            self.mqtt_port = int(data['port'])
            self.mqtt_username = data['certificateAccount']
            self.mqtt_password = data['certificatePassword']
            log.debug(f"MQTT credentials obtained - URL: {self.mqtt_url}:{self.mqtt_port}\nClientID: {self.mqtt_client_id}\nUsername: {self.mqtt_username}\nPassword: {self.mqtt_password}")
        else:
            raise Exception(f"Failed to get MQTT credentials: {response.text}")

    def get_json_response(self, request):
        if request.status_code != 200:
            raise Exception(f"Got HTTP status code {request.status_code}: {request.text}")

        try:
            response = json.loads(request.text)
            response_message = response["message"]
        except KeyError as key:
            raise Exception(f"Failed to extract key {key} from {response}")
        except Exception as error:
            raise Exception(f"Failed to parse response: {request.text} Error: {error}")

        if response_message.lower() != "success":
            raise Exception(f"{response_message}")

        return response


class EcoflowMQTT():
    def __init__(self, message_queue, device_sn, username, password, addr, port, client_id, timeout_seconds):
        self.message_queue = message_queue
        self.addr = addr
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.topic = f"/open/{username}/{device_sn}/quota"
        self.timeout_seconds = timeout_seconds
        self.last_message_time = None
        self.client = None

        self.connect()

        self.idle_timer = RepeatTimer(10, self.idle_reconnect)
        self.idle_timer.daemon = True
        self.idle_timer.start()

    def connect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, self.client_id)
        self.client.username_pw_set(self.username, self.password)
        self.client.tls_set(certfile=None, keyfile=None, cert_reqs=ssl.CERT_REQUIRED)
        self.client.tls_insecure_set(False)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        log.info(f"Connecting to MQTT Broker {self.addr}:{self.port} using client id {self.client_id}")
        self.client.connect(self.addr, self.port)
        self.client.loop_start()

    def idle_reconnect(self):
        if self.last_message_time and time.time() - self.last_message_time > self.timeout_seconds:
            log.error(f"No messages received for {self.timeout_seconds} seconds. Reconnecting to MQTT")
            # We pull the following into a separate process because there are actually quite a few things that can go
            # wrong inside the connection code, including it just timing out and never returning. So this gives us a
            # measure of safety around reconnection
            while True:
                connect_process = Process(target=self.connect)
                connect_process.start()
                connect_process.join(timeout=60)
                connect_process.terminate()
                if connect_process.exitcode == 0:
                    log.info("Reconnection successful, continuing")
                    # Reset last_message_time here to avoid a race condition between idle_reconnect getting called again
                    # before on_connect() or on_message() are called
                    self.last_message_time = None
                    break
                else:
                    log.error("Reconnection errored out, or timed out, attempted to reconnect...")

    def on_connect(self, client, userdata, flags, reason_code, properties):
        # Initialize the time of last message at least once upon connection so that other things that rely on that to be
        # set (like idle_reconnect) work
        self.last_message_time = time.time()
        match reason_code:
            case "Success":
                self.client.subscribe(self.topic)
                log.info(f"Subscribed to MQTT topic {self.topic}")
            case "Keep alive timeout":
                log.error("Failed to connect to MQTT: connection timed out")
            case "Unsupported protocol version":
                log.error("Failed to connect to MQTT: unsupported protocol version")
            case "Client identifier not valid":
                log.error("Failed to connect to MQTT: invalid client identifier")
            case "Server unavailable":
                log.error("Failed to connect to MQTT: server unavailable")
            case "Bad user name or password":
                log.error("Failed to connect to MQTT: bad username or password")
            case "Not authorized":
                log.error("Failed to connect to MQTT: not authorised")
            case _:
                log.error(f"Failed to connect to MQTT: another error occured: {reason_code}")

        return client

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code > 0:
            log.error(f"Unexpected MQTT disconnection: {reason_code}. Will auto-reconnect")
            time.sleep(5)

    def on_message(self, client, userdata, message):
        self.message_queue.put(message.payload.decode("utf-8"))
        self.last_message_time = time.time()


class EcoflowMetric:
    def __init__(self, ecoflow_payload_key, device_name):
        self.ecoflow_payload_key = ecoflow_payload_key
        self.device_name = device_name
        self.name = f"ecoflow_{self.convert_ecoflow_key_to_prometheus_name()}"
        self.is_version_metric = self.name.endswith('_ver') or self.name.endswith('_version')
        self.is_time_metric = self.ecoflow_payload_key in TIME_CONVERSIONS
        self.value = None
        self.last_update_time = None
        
        unit = self.get_unit()
        description = f"value from MQTT object key {ecoflow_payload_key}"
        if unit:
            description += f" (in {unit})"
        
        if self.is_version_metric:
            self.metric = Gauge(self.name, description, labelnames=["device", "formatted"])
        else:
            self.metric = Gauge(self.name, description, labelnames=["device"])


    def set(self, value):
        original_value = value

        if self.ecoflow_payload_key in DIVISORS:
            value /= DIVISORS[self.ecoflow_payload_key]
        
        if self.is_time_metric:
            if self.ecoflow_payload_key == 'ems.chgRemainTime' and value > 1e6:
                log.debug(f"Charging remaining time is very large: {value}. Setting to infinity.")
                value = float('inf')
            else:
                value *= TIME_CONVERSIONS[self.ecoflow_payload_key]

        if self.is_version_metric:
            major = int(value / 1000000)
            minor = int((value % 1000000) / 10000)
            patch = value % 10000
            formatted_value = f"{major}.{minor:02d}.{patch:05d}"
            log.debug(f"Set {self.name} = {formatted_value} (raw: {original_value})")
            self.metric.labels(device=self.device_name, formatted=formatted_value).set(value)
        else:
            log.debug(f"Set {self.name} = {value} (original: {original_value})")
            self.metric.labels(device=self.device_name).set(value)

        self.value = value
        self.last_update_time = time.time()


    def get_unit(self):
        if self.name.endswith('_vol'):
            return 'volts'
        elif self.name.endswith('_amp'):
            return 'amperes'
        elif 'watts' in self.name.lower():
            if self.ecoflow_payload_key in DIVISORS and DIVISORS[self.ecoflow_payload_key] == 1000:
                return 'kilowatts'
            return 'watts'
        elif 'temp' in self.name:
            return 'celsius'
        elif 'cap' in self.name:
            return 'ampere_hours'
        elif self.is_time_metric:
            return 'seconds'
        elif 'freq' in self.name:
            return 'hertz'
        elif self.name.endswith('_power'):
            if self.ecoflow_payload_key in DIVISORS and DIVISORS[self.ecoflow_payload_key] == 1000:
                return 'kilowatt_hours'
            return 'watt_hours'
        else:
            return None


    def convert_ecoflow_key_to_prometheus_name(self):
        # bms_bmsStatus.maxCellTemp -> bms_bms_status_max_cell_temp
        # pd.ext4p8Port -> pd_ext4p8_port
        key = self.ecoflow_payload_key.replace('.', '_')
        new = key[0].lower()
        for character in key[1:]:
            if character.isupper() and not new[-1] == '_':
                new += '_'
            new += character.lower()
        # Check that metric name complies with the data model for valid characters
        # https://prometheus.io/docs/concepts/data_model/#metric-names-and-labels
        if not re.match("[a-zA-Z_:][a-zA-Z0-9_:]*", new):
            raise EcoflowMetricException(f"Cannot convert payload key {self.ecoflow_payload_key} to comply with the Prometheus data model. Please, raise an issue!")
        return new

    def clear(self):
        log.debug(f"Clear {self.name}")
        if not self.is_time_metric or not self.ecoflow_payload_key.endswith('UsedTime'):
            self.metric.clear()
        self.last_update_time = time.time()

class Worker:
    def __init__(self, message_queue, device_name, collecting_interval_seconds=10, expiration_threshold=300):
        self.message_queue = message_queue
        self.device_name = device_name
        self.collecting_interval_seconds = collecting_interval_seconds
        self.expiration_threshold = expiration_threshold
        self.metrics_collector = []
        self.online = Gauge("ecoflow_online", "1 if device is online", labelnames=["device"])
        self.mqtt_messages_receive_total = Counter("ecoflow_mqtt_messages_receive_total", "total MQTT messages", labelnames=["device"])

    def loop(self):
        time.sleep(self.collecting_interval_seconds)
        while True:
            queue_size = self.message_queue.qsize()
            if queue_size > 0:
                log.info(f"Processing {queue_size} event(s) from the message queue")
                self.online.labels(device=self.device_name).set(1)
                self.mqtt_messages_receive_total.labels(device=self.device_name).inc(queue_size)
            else:
                log.info("Message queue is empty. Assuming that the device is offline")
                self.online.labels(device=self.device_name).set(0)
                # Clear metrics for NaN (No data) instead of last value
                for metric in self.metrics_collector:
                    metric.clear()

            while not self.message_queue.empty():
                payload = self.message_queue.get()
                log.debug(f"Received payload: {payload}")
                if payload is None:
                    continue

                try:
                    payload = json.loads(payload)
                    params = payload['params']
                except KeyError as key:
                    log.error(f"Failed to extract key {key} from payload: {payload}")
                except Exception as error:
                    log.error(f"Failed to parse MQTT payload: {payload} Error: {error}")
                    continue
                self.process_payload(params)

            self.clear_expired_metrics()
            time.sleep(self.collecting_interval_seconds)

    def clear_expired_metrics(self):
        current_time = time.time()
        for metric in self.metrics_collector:
            if current_time - metric.last_update_time > self.expiration_threshold:
                metric.clear()
                log.info(f"Cleared expired metric {metric.name}")

    def get_metric_by_ecoflow_payload_key(self, ecoflow_payload_key):
        for metric in self.metrics_collector:
            if metric.ecoflow_payload_key == ecoflow_payload_key:
                log.debug(f"Found metric {metric.name} linked to {ecoflow_payload_key}")
                return metric
        log.debug(f"Cannot find metric linked to {ecoflow_payload_key}")
        return False

    def process_payload(self, params):
        log.debug(f"Processing params: {params}")
        for ecoflow_payload_key in params.keys():
            ecoflow_payload_value = params[ecoflow_payload_key]
            if not isinstance(ecoflow_payload_value, (int, float)):
                log.warning(f"Skipping unsupported metric {ecoflow_payload_key}: {ecoflow_payload_value}")
                continue

            metric = self.get_metric_by_ecoflow_payload_key(ecoflow_payload_key)
            if not metric:
                try:
                    metric = EcoflowMetric(ecoflow_payload_key, self.device_name)
                except EcoflowMetricException as error:
                    log.error(error)
                    continue
                log.info(f"Created new metric from payload key {metric.ecoflow_payload_key} -> {metric.name}")
                self.metrics_collector.append(metric)

            metric.set(ecoflow_payload_value)

            if ecoflow_payload_key == 'inv.acInVol' and ecoflow_payload_value == 0:
                ac_in_current = self.get_metric_by_ecoflow_payload_key('inv.acInAmp')
                if ac_in_current:
                    log.debug("Set AC inverter input current to zero because of zero inverter voltage")
                    ac_in_current.set(0)


def signal_handler(signum, frame):
    log.info(f"Received signal {signum}. Exiting...")
    sys.exit(0)

def main():
    # Register the signal handler for SIGTERM
    signal.signal(signal.SIGTERM, signal_handler)

    # Disable Process and Platform collectors
    for coll in list(REGISTRY._collector_to_names.keys()):
        REGISTRY.unregister(coll)

    log_level = os.getenv("LOG_LEVEL", "INFO")

    match log_level:
        case "DEBUG":
            log_level = log.DEBUG
        case "INFO":
            log_level = log.INFO
        case "WARNING":
            log_level = log.WARNING
        case "ERROR":
            log_level = log.ERROR
        case _:
            log_level = log.INFO

    log.basicConfig(stream=sys.stdout, level=log_level, format='%(asctime)s %(levelname)-7s %(message)s')

    device_sn = os.getenv("DEVICE_SN")
    device_name = os.getenv("DEVICE_NAME") or device_sn
    # If you are using MQTT to connect to the API be aware that only 10 unique client IDs are allowed per day. 
    # As such, it is suggested that you choose a static client_id for your application or integration to use consistently. 
    # If your code generates a unique client_id (as mine did) for each connection,
    # you can exceed this limit very quickly when testing or debugging code.
    client_id = os.getenv("CLIENT_ID") or f"ecoflow_exporter_{device_sn}_{datetime.now().strftime('%Y%m%d')}"
    access_key = os.getenv("ECOFLOW_ACCESS_KEY")
    secret_key = os.getenv("ECOFLOW_SECRET_KEY")
    ecoflow_api_host = os.getenv("ECOFLOW_API_HOST", "api.ecoflow.com")
    exporter_port = int(os.getenv("EXPORTER_PORT", "9090"))
    collecting_interval_seconds = int(os.getenv("COLLECTING_INTERVAL", "10"))
    timeout_seconds = int(os.getenv("MQTT_TIMEOUT", "60"))

    if (not device_sn or not access_key or not secret_key):
        log.error("Please, provide all required environment variables: DEVICE_SN, ECOFLOW_ACCESS_KEY, ECOFLOW_SECRET_KEY")
        sys.exit(1)

    try:
        auth = EcoflowAuthentication(client_id, access_key, secret_key, ecoflow_api_host)
    except Exception as error:
        log.error(error)
        sys.exit(1)

    message_queue = Queue()

    EcoflowMQTT(message_queue, device_sn, auth.mqtt_username, auth.mqtt_password, auth.mqtt_url, auth.mqtt_port, auth.mqtt_client_id, timeout_seconds)

    metrics = Worker(message_queue, device_name, collecting_interval_seconds)

    start_http_server(exporter_port)

    try:
        metrics.loop()

    except KeyboardInterrupt:
        log.info("Received KeyboardInterrupt. Exiting...")
        sys.exit(0)


if __name__ == '__main__':
    main()
