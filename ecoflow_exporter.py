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
from queue import Queue, Empty
from threading import Event
import paho.mqtt.client as mqtt
from prometheus_client import start_http_server, REGISTRY, Gauge, Counter
import requests

class EcoflowMetricException(Exception):
    pass

class EcoflowAuthentication:
    def __init__(self, access_key, secret_key, api_host):
        self.access_key = access_key
        self.secret_key = secret_key
        self.api_host = api_host
        self.mqtt_url = None
        self.mqtt_port = None
        self.mqtt_username = None
        self.mqtt_password = None

    def get_mqtt_credentials(self):
        url = f"https://{self.api_host}/iot-open/sign/certification"
        timestamp = str(int(time.time() * 1000))
        nonce = base64.b64encode(hashlib.sha256(timestamp.encode()).digest()).decode()[:-1]
        to_sign = f'accessKey={self.access_key}&nonce={nonce}&timestamp={timestamp}'
        sign = hmac.new(self.secret_key.encode(), to_sign.encode(), hashlib.sha256).hexdigest()

        headers = {
            'accessKey': self.access_key,
            'timestamp': timestamp,
            'nonce': nonce,
            'sign': sign
        }

        log.debug(f"Sending authentication request to {url}")
        log.debug(f"Request headers: {headers}")

        response = requests.get(url, headers=headers)
        log.debug(f"Authentication response status code: {response.status_code}")
        log.debug(f"Authentication response content: {response.text}")

        if response.status_code == 200:
            data = response.json()['data']
            self.mqtt_url = data['url']
            self.mqtt_port = int(data['port'])
            self.mqtt_username = data['certificateAccount']
            self.mqtt_password = data['certificatePassword']
            log.debug(f"MQTT credentials obtained - URL: {self.mqtt_url}, Port: {self.mqtt_port}, Username: {self.mqtt_username}")
        else:
            raise Exception(f"Failed to get MQTT credentials: {response.text}")

class EcoflowMQTT:
    def __init__(self, message_queue, auth: EcoflowAuthentication, device_sn):
        self.message_queue = message_queue
        self.auth = auth
        self.device_sn = device_sn
        self.connected = False
        self.client = None
        self.stop_event = Event()

        log.debug("Initializing EcoflowMQTT")
        self.auth.get_mqtt_credentials()
        self.connect()

    def connect(self):
        if self.client:
            log.debug("Stopping existing MQTT client")
            self.client.loop_stop()
            self.client.disconnect()

        log.debug("Creating new MQTT client")
        self.client = mqtt.Client(client_id="", clean_session=True, protocol=mqtt.MQTTv311, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.client.username_pw_set(self.auth.mqtt_username, self.auth.mqtt_password)
        self.client.tls_set(certfile=None, keyfile=None, cert_reqs=ssl.CERT_REQUIRED)
        self.client.tls_insecure_set(False)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        log.info(f"Connecting to MQTT Broker {self.auth.mqtt_url}:{self.auth.mqtt_port}")
        try:
            self.client.connect(self.auth.mqtt_url, self.auth.mqtt_port)
            log.debug("MQTT connect() call successful")
        except Exception as e:
            log.error(f"Failed to connect to MQTT broker: {e}")
        self.client.loop_start()
        log.debug("MQTT client loop started")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        log.debug(f"MQTT on_connect callback - rc: {rc}, flags: {flags}")
        if rc == 0:
            self.connected = True
            log.info("Connected to Ecoflow MQTT Server")
            topic = f"/open/{self.auth.mqtt_username}/{self.device_sn}/quota"
            self.client.subscribe(topic)
            log.info(f"Subscribed to MQTT topic {topic}")
        else:
            log.error(f"Failed to connect to MQTT: {mqtt.connack_string(rc)}")

    def on_disconnect(self, client, userdata, rc, properties=None):
        log.debug(f"MQTT on_disconnect callback - rc: {rc}")
        if rc != 0:
            log.error(f"Unexpected MQTT disconnection: {rc}. Will auto-reconnect")

    def on_message(self, client, userdata, message):
        log.debug(f"Received MQTT message on topic: {message.topic}")
        self.message_queue.put(message.payload.decode("utf-8"))

    def stop(self):
        log.debug("Stopping EcoflowMQTT")
        self.stop_event.set()
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

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
    def __init__(self, message_queue, device_name):
        self.message_queue = message_queue
        self.device_name = device_name
        self.metrics_collector = []
        self.online = Gauge("ecoflow_online", "1 if device is online", labelnames=["device"])
        self.mqtt_messages_receive_total = Counter("ecoflow_mqtt_messages_receive_total", "total MQTT messages", labelnames=["device"])
        self.stop_event = Event()

    def loop(self):
        self.online.labels(device=self.device_name).set(1)
        while not self.stop_event.is_set():
            try:
                payload = self.message_queue.get(timeout=1)
                self.mqtt_messages_receive_total.labels(device=self.device_name).inc()
                log.debug(f"Received payload: {payload}")

                try:
                    payload_json = json.loads(payload)
                    params = payload_json['params']
                    self.process_payload(params)
                except KeyError as key:
                    log.error(f"Failed to extract key {key} from payload: {payload}")
                except json.JSONDecodeError:
                    log.error(f"Failed to parse MQTT payload: {payload}")
                except Exception as error:
                    log.error(f"Error processing payload: {error}")
            except Empty:
                # This is expected, it allows us to check the stop_event regularly
                pass

        self.online.labels(device=self.device_name).set(0)

    def get_metric_by_ecoflow_payload_key(self, ecoflow_payload_key):
        for metric in self.metrics_collector:
            if metric.ecoflow_payload_key == ecoflow_payload_key:
                log.debug(f"Found metric {metric.name} linked to {ecoflow_payload_key}")
                return metric
        log.debug(f"Cannot find metric linked to {ecoflow_payload_key}")
        return False

    def process_payload(self, params):
        log.debug(f"Processing params: {params}")
        for ecoflow_payload_key, ecoflow_payload_value in params.items():
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

    def stop(self):
        self.stop_event.set()

def signal_handler(signum, frame):
    log.info(f"Received signal {signum}. Exiting...")
    sys.exit(0)

def main():
    signal.signal(signal.SIGTERM, signal_handler)

    for coll in list(REGISTRY._collector_to_names.keys()):
        REGISTRY.unregister(coll)

    log_level = os.getenv("LOG_LEVEL", "INFO")
    log_level = getattr(log, log_level, log.INFO)
    log.basicConfig(stream=sys.stdout, level=log_level, format='%(asctime)s %(levelname)-7s %(message)s')

    device_sn = os.getenv("DEVICE_SN")
    device_name = os.getenv("DEVICE_NAME") or device_sn
    access_key = os.getenv("ECOFLOW_ACCESS_KEY")
    secret_key = os.getenv("ECOFLOW_SECRET_KEY")
    api_host = os.getenv("ECOFLOW_API_HOST", "api-e.ecoflow.com")
    exporter_port = int(os.getenv("EXPORTER_PORT", "9090"))

    if not all([device_sn, access_key, secret_key]):
        log.error("Please provide all required environment variables: DEVICE_SN, ECOFLOW_ACCESS_KEY, ECOFLOW_SECRET_KEY")
        sys.exit(1)

    log.debug(f"Initializing with Device SN: {device_sn}, Device Name: {device_name}, API Host: {api_host}")

    auth = EcoflowAuthentication(access_key, secret_key, api_host)
    message_queue = Queue()

    mqtt_client = EcoflowMQTT(message_queue, auth, device_sn)
    worker = Worker(message_queue, device_name)

    start_http_server(exporter_port)
    log.info(f"HTTP server started on port {exporter_port}")

    try:
        worker.loop()
    except KeyboardInterrupt:
        log.info("Received KeyboardInterrupt. Exiting...")
    finally:
        mqtt_client.stop()
        worker.stop()
        log.info("Exporter stopped")

if __name__ == '__main__':
    main()
