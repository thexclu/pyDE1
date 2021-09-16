"""
Copyright © 2021 Jeff Kletsky. All Rights Reserved.

License for this software, part of the pyDE1 package, is granted under
GNU General Public License v3.0 only
SPDX-License-Identifier: GPL-3.0-only

Replays the captured packets from a flow sequence
such as for testing or demonstration of "GUIs"

NB: Does _not_ modify the DB contents, so real-time pulls by the consumer
    will be "at completion", not "as they were".

    This primarily impacts the sequence table, which is updated
    as the sequence unfolds.
"""

import json
import logging
import os
import socket
import sqlite3
import time

from typing import NamedTuple, Union, List, Optional

import paho.mqtt.client as mqtt
from paho.mqtt.client import MQTTv5, MQTT_CLEAN_START_FIRST_ONLY

from pyDE1.config_toml import ConfigToml


class Config (ConfigToml):

    DEFAULT_CONFIG_FILE = '/usr/local/etc/pyde1/pyde1-replay.conf'

    def __init__(self):
        super(Config, self).__init__()
        self.database = self._Database()
        self.logging = self._Logging()
        self.mqtt = self._MQTT()
        self.sequence = self._Sequence()

    # This craziness is so pyCharm autocompletes
    # Otherwise typing.SimpleNamespace() would be sufficient

    class _MQTT (ConfigToml._Loadable):
        def __init__(self):
            self.TOPIC_ROOT = 'KEpyDE1'
            self.CLIENT_ID_PREFIX = 'pyde1-replay'
            self.BROKER_HOSTNAME = '::1'
            self.BROKER_PORT = 1883
            self.TRANSPORT = 'tcp'
            self.TLS_CONTEXT = None
            self.KEEPALIVE = 60
            self.USERNAME = None
            self.PASSWORD = None
            self.DEBUG = False

    class _Logging (ConfigToml._Loadable):
        def __init__(self):
            self.LOG_DIRECTORY = '/var/log/pyde1/'
            # NB: The log file name is matched against [a-zA-Z0-9._-]
            self.LOG_FILENAME = 'replay.log'
            self.FORMAT_MAIN = "%(asctime)s %(levelname)s " \
                               "%(name)s: %(message)s"
            self.FORMAT_STDERR = self.FORMAT_MAIN
            self.LEVEL_MAIN = logging.DEBUG
            self.LEVEL_STDERR = logging.DEBUG
            self.LEVEL_MQTT = logging.INFO

    def set_logging(self):
        # TODO: Clean up logging, in general
        # TODO: Consider replacing this with logging.config.fileConfig()
        formatter_main = logging.Formatter(fmt=config.logging.FORMAT_MAIN)
        formatter_stderr = logging.Formatter(fmt=config.logging.FORMAT_STDERR)
        root_logger = logging.getLogger()
        root_logger.setLevel(self.logging.LEVEL_MAIN)
        for handler in root_logger.handlers:
            try:
                if isinstance(handler, logging.StreamHandler) \
                        and handler.stream.name == '<stderr>':
                    handler.setLevel(self.logging.LEVEL_STDERR)
                    handler.setFormatter(formatter_stderr)
            except AttributeError:
                pass

    class _Database (ConfigToml._Loadable):
        def __init__(self):
            self.FILENAME = '/var/lib/pyde1/pyde1.sqlite3'

    class _Sequence (ConfigToml._Loadable):
        def __init__(self):
            self.ID = None


config = Config()


# TODO: Figure out how not to duplicate this in so many places

# NB: Remember to reconstruct the class element

class SequenceRow (NamedTuple):
    id: str
    active_state:   str
    start_sequence: float
    start_flow:     float
    end_flow:       float
    end_sequence:   float
    profile_id:     str
    # https://www.sqlite.org/quirks.html#no_separate_boolean_datatype
    profile_assumed:    int     # 0: False, 1: True
    resource_version:                           str
    resource_de1_id:                            str
    resource_de1_read_once:                     str
    resource_de1_calibration_flow_multiplier:   str
    resource_de1_control_mode:                  str
    resource_de1_control_tank_water_threshold:  str
    resource_de1_setting_before_flow:           str
    resource_de1_setting_steam:                 str
    resource_de1_setting_target_group_temp:     str
    resource_scale_id:                          str

    @property
    def class_str(self):
        return None


def sequence_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return SequenceRow(*row)


class ShotSampleRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    de1_time:           float
    sample_time:        int
    group_pressure:     float
    group_flow:         float
    mix_temp:           float
    head_temp:          float
    set_mix_temp:       float
    set_head_temp:      float
    set_group_pressure: float
    set_group_flow:     float
    frame_number:       int
    steam_temp:         float
    volume_preinfuse:   float
    volume_pour:        float
    volume_total:       float
    volume_by_frames:   str     # representation of list

    @property
    def class_str(self):
        return 'ShotSampleWithVolumesUpdate'


def shot_sample_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return ShotSampleRow(*row)


class WeightFlowRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    scale_time:             float
    current_weight:         float
    current_weight_time:    float
    average_flow:           float
    average_flow_time:      float
    median_weight:          float
    median_weight_time:     float
    median_flow:            float
    median_flow_time:       float

    @property
    def class_str(self):
        return 'WeightAndFlowUpdate'


def weight_flow_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return WeightFlowRow(*row)


class StateUpdateRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    event_time:         float
    state:              str
    substate:           str
    previous_state:     str
    previous_substate:  str
    is_error_state:     str     # TODO: Fix this in schema and access

    @property
    def class_str(self):
        return 'StateUpdate'


def state_update_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return StateUpdateRow(*row)


class SequencerGateNotificationRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    name:           str
    action:         str
    active_state:   str
    sequence_id:    str

    @property
    def class_str(self):
        return 'SequencerGateNotification'


def sequence_gate_notification_row_factory(cur: sqlite3.Cursor,
                                           row: sqlite3.Row):
    return SequencerGateNotificationRow(*row)


class WaterLevelRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    level:              float
    start_fill_level:   float

    @property
    def class_str(self):
        return 'WaterLevelUpdate'


def water_level_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return WaterLevelRow(*row)


# NB: Database does not yet not include name or ID

class OldConnectivityChangeRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    state:              str
    # name:               str
    # id:                 str


def old_connectivity_change_row_factory(cur: sqlite3.Cursor,
                                        row: sqlite3.Row):
    return OldConnectivityChangeRow(*row)


class ConnectivityChangeRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    state:              str
    name:               str
    id:                 str

    @property
    def class_str(self):
        return 'ConnectivityChange'


def augment_old_connectivity_change_row(
        old_row: OldConnectivityChangeRow) -> ConnectivityChangeRow:

    if old_row.sender == 'DE1':
        id = 'D9:B2:48:aa:bb:cc'
        name = 'DE1'
    else:
        id = 'CF:75:75:aa:bb::cc'
        name = 'Skale'

    return ConnectivityChangeRow(*old_row, name, id)


class SendListEntry (NamedTuple):
    send_at:    float
    payload:    str


def _shift_if_time(key: str, val: Union[str, float], shift: float):
    if key.endswith('_time'):
        return val + shift
    else:
        return val


def create_entry(row: NamedTuple, shift_time: float) -> SendListEntry:
    """
    Shift all time elements by adding shift_time
    Return a JSON string, so compatible with api/mqtt/run
        outbound_pipe_reader()
            item_json = outbound_pipe.recv()
    """

    row_dict = {k:_shift_if_time(k, v, shift_time)
                for (k,v) in row._asdict().items()}
    # For now, bomb out on missing property
    row_dict['class'] = row.class_str
    row_dict['shifted'] = shift_time

    return SendListEntry(send_at=row_dict['event_time'],
                         payload=json.dumps(row_dict))


def collect_send_list(sequence_id: str,
                      shift_time: float) -> List[SendListEntry]:
    send_list = []

    with sqlite3.connect(f"file:{config.database.FILENAME}?mode=ro",
                         uri=True) as db:

        db.row_factory = shot_sample_row_factory
        cur = db.execute(f"SELECT {' ,'.join(ShotSampleRow._fields)} "
                         "FROM shot_sample_with_volume_update "
                         "WHERE sequence_id == :id "
                         "ORDER BY event_time",
                         {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

        db.row_factory = weight_flow_row_factory
        cur = db.execute(f"SELECT {' ,'.join(WeightFlowRow._fields)} "
                         "FROM weight_and_flow_update "
                         "WHERE sequence_id == :id "
                         "ORDER BY event_time",
                         {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

        db.row_factory = state_update_row_factory
        cur = db.execute(f"SELECT {' ,'.join(StateUpdateRow._fields)} "
                         "FROM state_update "
                         "WHERE sequence_id == :id "
                         "ORDER BY event_time",
                         {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

        db.row_factory = water_level_row_factory
        cur = db.execute(f"SELECT {' ,'.join(WaterLevelRow._fields)} "
                         "FROM water_level_update "
                         "WHERE sequence_id == :id "
                         "ORDER BY event_time",
                         {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

        db.row_factory = old_connectivity_change_row_factory
        cur = db.execute(
            f"SELECT {' ,'.join(OldConnectivityChangeRow._fields)} "
            "FROM connectivity_change "
            "WHERE sequence_id == :id "
            "ORDER BY event_time",
            {'id': sequence_id})
        for row in cur.fetchall():
            new_row = augment_old_connectivity_change_row(row)
            send_list.append(create_entry(new_row, shift_time))

        db.row_factory = sequence_gate_notification_row_factory
        cur = db.execute(
            f"SELECT {' ,'.join(SequencerGateNotificationRow._fields)} "
            "FROM sequencer_gate_notification "
            "WHERE sequence_id == :id "
            "ORDER BY event_time",
            {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

    send_list.sort(key=lambda entry: entry.send_at)
    return send_list


def get_sequence_start_time(sequence_id: str) -> float:

    with sqlite3.connect(f"file:{config.database.FILENAME}?mode=ro",
                         uri=True) as db:
        db.row_factory = sequence_row_factory
        cur = db.execute(f"SELECT {', '.join(SequenceRow._fields)} "
                         "FROM sequence "
                         "WHERE id == :id",
                         (sequence_id,))
        row = cur.fetchone()
        return row.start_sequence


# MQTT

def setup_client(mqtt_client_logger: logging.Logger) -> mqtt.Client:

    def on_log_callback(client: mqtt.Client, userdata, level, buf):
        mqtt_client_logger.info(f"CB: Log: level: {level} '{buf}' ({type(buf)})")

    def on_connect_callback(client, userdata, flags, reasonCode, properties):
        mqtt_client_logger.info(
            f"CB: Connect: flags: {flags}, reasonCode: {reasonCode}, "
            f"properties {properties}")

    def on_publish_callback(client, userdata, mid):
        mqtt_client_logger.info(f"CB: Published: mid: {mid}")

    # Caught exception in on_disconnect:
    #     on_disconnect_callback() missing 1 required positional argument:
    #         'properties'
    def on_disconnect_callback(client, userdata, reasonCode, properties=None):
        mqtt_client_logger.info(f"CB: Disconnect: reasonCode: {reasonCode}, "
                           f"properties {properties}")

    def on_socket_open_callback(client, userdata, socket):
        mqtt_client_logger.info(f"CB: Socket open: socket: {socket}")

    def on_socket_close_callback(client, userdata, socket):
        mqtt_client_logger.info(f"CB: Socket close: socket: {socket}")

    def on_socket_register_write_callback(client, userdata, socket):
        mqtt_client_logger.info(f"CB: Socket register write: socket: {socket}")

    def on_socket_unregister_write_callback(client, userdata, socket):
        mqtt_client_logger.info(f"CB: Socket unregister write: socket: {socket}")

    mqtt_client = mqtt.Client(
        client_id="{}@{}[{}]".format(
            config.mqtt.CLIENT_ID_PREFIX,
            socket.gethostname(),
            os.getpid(),
        ),
        clean_session=None,  # Required for MQTT5
        userdata=None,
        protocol=MQTTv5,
        transport=config.mqtt.TRANSPORT,
    )

    if config.mqtt.USERNAME is not None:
        mqtt_client_logger.info(
            f"Connecting with username '{config.mqtt.USERNAME}'")
        mqtt_client.username_pw_set(
            username=config.mqtt.USERNAME,
            password=config.mqtt.PASSWORD
        )

    # mqtt_client.on_log = on_log_callback
    mqtt_client.on_connect = on_connect_callback
    # mqtt_client.on_publish = on_publish_callback
    mqtt_client.on_disconnect = on_disconnect_callback
    mqtt_client.on_socket_open = on_socket_open_callback
    mqtt_client.on_socket_close = on_socket_close_callback
    # mqtt_client.on_socket_register_write = on_socket_register_write_callback
    # mqtt_client.on_socket_unregister_write = on_socket_unregister_write_callback

    mqtt_client.enable_logger(mqtt_client_logger)

    mqtt_client.connect(host=config.mqtt.BROKER_HOSTNAME,
                   port=config.mqtt.BROKER_PORT,
                   keepalive=config.mqtt.KEEPALIVE,
                   bind_address="",
                   bind_port=0,
                   clean_start=MQTT_CLEAN_START_FIRST_ONLY,
                   properties=None)

    return mqtt_client


if __name__ == '__main__':

    import argparse

    ap = argparse.ArgumentParser(
        description=
        """Replay a sequence from the database, time shifted to the present.
        
        """
        f"Default configuration file is at {config.DEFAULT_CONFIG_FILE}"
    )
    ap.add_argument('-c', type=str, help='Use as alternate config file')
    ap.add_argument('-s', type=str, help='Override for sequence ID')
    ap.add_argument('-t', type=str, help='Override for MQTT topic')

    args = ap.parse_args()

    config.load_from_toml(args.c)

    if args.s is not None:
        config.sequence.ID = args.s

    if args.t is not None:
        config.mqtt.TOPIC_ROOT = args.t

    config.set_logging()

    client_logger = logging.getLogger('MQTT')
    client_logger.level = logging.ERROR

    sst = get_sequence_start_time(config.sequence.ID)
    now = time.time()
    start_sequence_at = now + 5
    shift_time = start_sequence_at - sst
    send_list = collect_send_list(config.sequence.ID, shift_time)
    mqtt_client = setup_client(client_logger)
    mqtt_client.loop_start()

    MQTT_LEAD_TIME = 0.000  # seconds

    while len(send_list):
        next_to_send = send_list.pop(0)
        while next_to_send.send_at > time.time() + MQTT_LEAD_TIME:
            time.sleep(0.010)
        print(time.time(), next_to_send)
        item_as_dict = json.loads(next_to_send.payload)
        topic = f"{config.mqtt.TOPIC_ROOT}/{item_as_dict['class']}"
        mqtt_client.publish(
            topic=topic,
            payload=next_to_send.payload,
            qos=0,
            retain=False,
            properties=None
        )

    # Have to let the last message drain before existing
    time.sleep(1)




