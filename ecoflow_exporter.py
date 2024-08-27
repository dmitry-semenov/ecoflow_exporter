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
from queue import Queue
from threading import Timer
from multiprocessing import Process
import paho.mqtt.client as mqtt
from prometheus_client import start_http_server, REGISTRY, Gauge, Counter

class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)

class EcoflowMetricException(Exception):
    pass

class EcoflowMQTT():
    def __init__(self, message_queue, device_sn, access_key, secret_key, addr, port, timeout_seconds):
        self.message_queue = message_queue
        self.addr = addr
        self.port = port
        self.device_sn = device_sn
        self.access_key = access_key
        self.secret_key = secret_key
        self.topic = f"/open/{access_key}/{device_sn}/quota"
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

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.tls_set(certfile=None, keyfile=None, cert_reqs=ssl.CERT_REQUIRED)
        self.client.tls_insecure_set(False)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        # Generate authentication parameters
        timestamp = str(int(time.time() * 1000))
        nonce = base64.b64encode(hashlib.sha256(timestamp.encode()).digest()).decode()[:-1]
        to_sign = f'accessKey={self.access_key}&nonce={nonce}&timestamp={timestamp}'
        sign = hmac.new(self.secret_key.encode(), to_sign.encode(), hashlib.sha256).hexdigest()

        # Set authentication headers
        self.client.username_pw_set(username=self.access_key, password=self.secret_key)
        self.client._username = self.access_key
        self.client._password = self.secret_key
        self.client._connect_properties = mqtt.Properties(
            UserProperty=[
                ("accessKey", self.access_key),
                ("timestamp", timestamp),
                ("nonce", nonce),
                ("sign", sign)
            ]
        )

        log.info(f"Connecting to MQTT Broker {self.addr}:{self.port}")
        self.client.connect(self.addr, self.port)
        self.client.loop_start()

    def idle_reconnect(self):
        if self.last_message_time and time.time() - self.last_message_time > self.timeout_seconds:
            log.error(f"No messages received for {self.timeout_seconds} seconds. Reconnecting to MQTT")
            while True:
                connect_process = Process(target=self.connect)
                connect_process.start()
                connect_process.join(timeout=60)
                connect_process.terminate()
                if connect_process.exitcode == 0:
                    log.info("Reconnection successful, continuing")
                    self.last_message_time = None
                    break
                else:
                    log.error("Reconnection errored out, or timed out, attempted to reconnect...")

    def on_connect(self, client, userdata, flags, reason_code, properties):
        self.last_message_time = time.time()
        if reason_code == 0:
            self.client.subscribe(self.topic)
            log.info(f"Subscribed to MQTT topic {self.topic}")
        else:
            log.error(f"Failed to connect to MQTT: {mqtt.connack_string(reason_code)}")

    def on_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.error(f"Unexpected MQTT disconnection. Will auto-reconnect")

    def on_message(self, client, userdata, message):
        self.message_queue.put(message.payload.decode("utf-8"))
        self.last_message_time = time.time()

class EcoflowMetric:
    def __init__(self, ecoflow_payload_key, device_name):
        self.ecoflow_payload_key = ecoflow_payload_key
        self.device_name = device_name
        self.name = f"ecoflow_{self.convert_ecoflow_key_to_prometheus_name()}"
        self.metric = Gauge(self.name, f"value from MQTT object key {ecoflow_payload_key}", labelnames=["device"])

    def convert_ecoflow_key_to_prometheus_name(self):
        key = self.ecoflow_payload_key.replace('.', '_')
        new = key[0].lower()
        for character in key[1:]:
            if character.isupper() and not new[-1] == '_':
                new += '_'
            new += character.lower()
        if not re.match("[a-zA-Z_:][a-zA-Z0-9_:]*", new):
            raise EcoflowMetricException(f"Cannot convert payload key {self.ecoflow_payload_key} to comply with the Prometheus data model. Please, raise an issue!")
        return new

    def set(self, value):
        log.debug(f"Set {self.name} = {value}")
        self.metric.labels(device=self.device_name).set(value)

    def clear(self):
        log.debug(f"Clear {self.name}")
        self.metric.clear()

class Worker:
    def __init__(self, message_queue, device_name, collecting_interval_seconds=10):
        self.message_queue = message_queue
        self.device_name = device_name
        self.collecting_interval_seconds = collecting_interval_seconds
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

            time.sleep(self.collecting_interval_seconds)

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
                try:
                    ecoflow_payload_value = float(ecoflow_payload_value)
                except ValueError:
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
    signal.signal(signal.SIGTERM, signal_handler)

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
    access_key = os.getenv("ECOFLOW_ACCESS_KEY")
    secret_key = os.getenv("ECOFLOW_SECRET_KEY")
    mqtt_host = os.getenv("MQTT_HOST", "mqtt.ecoflow.com")
    mqtt_port = int(os.getenv("MQTT_PORT", "8883"))
    exporter_port = int(os.getenv("EXPORTER_PORT", "9090"))
    collecting_interval_seconds = int(os.getenv("COLLECTING_INTERVAL", "10"))
    timeout_seconds = int(os.getenv("MQTT_TIMEOUT", "60"))

    if not all([device_sn, access_key, secret_key]):
        log.error("Please provide all required environment variables: DEVICE_SN, ECOFLOW_ACCESS_KEY, ECOFLOW_SECRET_KEY")
        sys.exit(1)

    message_queue = Queue()

    EcoflowMQTT(message_queue, device_sn, access_key, secret_key, mqtt_host, mqtt_port, timeout_seconds)

    metrics = Worker(message_queue, device_name, collecting_interval_seconds)

    start_http_server(exporter_port)

    try:
        metrics.loop()
    except KeyboardInterrupt:
        log.info("Received KeyboardInterrupt. Exiting...")
        sys.exit(0)

if __name__ == '__main__':
    main()
