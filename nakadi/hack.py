#!/usr/bin/env python3
import datetime
import gzip
import connexion
import flask
import json
import logging
import traceback
from time import sleep
from kafka import SimpleConsumer
from kafka.common import KafkaError
from kafka.producer.base import Producer
from nakadi.utils.utils import string_hashcode
from io import BytesIO

logging.basicConfig(level=logging.INFO)

from nakadi import event_stream, kafka_pool, monitoring, config, kafka_consumer_patch
from nakadi.security import authenticate
from nakadi.metrics import measured, metrics_writer
from nakadi.utils.request_helpers import NotIntegerParameterException, RequiredParameterNotFoundException, \
    WrongCursorsFormatException, get_int_parameter, get_cursors


def retry_if_failed(fn, *args, retry_limit = 5, retry_wait_s = 1, **kwargs):
    retry_attempts = 0

    while retry_attempts < retry_limit:
        try:
            with kafka_client_pool.kafka_client() as client:
                call_result = fn(client, *args, **kwargs)

            if retry_attempts > 0:
                logging.info('[#KFKRETRY] Made %s attempts to make kafka call', retry_attempts)
            return call_result

        except KafkaError:
            logging.error('[#KFKFAIL] Error on kafka communication. Will reset connection to retry after waiting %s s', retry_wait_s)
            logging.error(traceback.format_exc())
            sleep(retry_wait_s)
            retry_attempts += 1

    raise KafkaError()

@measured('get_topics')
@authenticate
def get_topics():
    try:
        topics = [{ 'name' : topic.decode('utf-8') } for topic in retry_if_failed(__get_kafka_topics)]
        return topics, 200
    except:
        return {'detail': 'System temporary not available'}, 503


def __get_kafka_topics(client):
    return client.topics


def __topic_exists(topic):
    topics = [t.decode('utf-8') for t in retry_if_failed(__get_kafka_topics)]
    return topic in topics


def __partition_exists(topic, partition_id):
    if not __topic_exists(topic):
        return False
    topic_partitions = retry_if_failed(__get_partitions, topic)
    return partition_id in topic_partitions


def __get_partitions(client, topic):
    return client.get_partition_ids_for_topic(topic)

@measured('get_partition')
@authenticate
def get_partition(topic, partition):

    # check that partition is integer
    if not partition.isdigit():
        return {'detail': '"partition" path parameter should be an integer number'}, 400
    else:
        partition = int(partition)

    if not __topic_exists(topic):
        return {'detail': 'topic not found'}, 404
    if not __partition_exists(topic, partition):
        return {'detail': 'partition not found'}, 404

    try:
        offsets = __get_partitions_offsets(topic)
    except:
        return {'detail': 'Not Available'}, 503

    partition_offsets = next(offset for offset in offsets if offset.get('partition_id') == partition)
    return partition_offsets, 200


@measured('get_partitions')
@authenticate
def get_partitions(topic):

    if not __topic_exists(topic):
        return {'detail': 'topic not found'}, 404

    try:
        offsets = __get_partitions_offsets(topic)
    except:
        return {'detail': 'Not Available'}, 503

    return offsets, 200


def __get_partitions_offsets(topic):
    # create dummy consumer to read partitions offsets
    try:
        consumer = retry_if_failed(SimpleConsumer, "dummy-group", topic)
    except:
        raise Exception('Kafka failed')

    # scroll to the oldest offsets and grab them
    consumer.seek(offset=0, whence=0)
    oldest_offsets = consumer.offsets.copy()

    # scroll to the newest offsets and grab them
    consumer.seek(offset=0, whence=2)
    newest_offsets = consumer.offsets.copy()
    consumer.stop()

    # generate result dictionary
    partition_offsets = list(map(lambda partition_id:
                                 {
                                     "partition_id": partition_id,
                                     "oldest_available_offset": oldest_offsets.get(partition_id),
                                     "newest_available_offset": newest_offsets.get(partition_id)
                                 },
                                 oldest_offsets.keys()))
    return partition_offsets


@measured('get_events_from_single_partition')
@authenticate
def get_events_from_single_partition(topic, partition):

    # check if topic exists
    if not __topic_exists(topic):
        return {'detail': 'topic not found'}, 404

    # create cursor for single partition
    try:
        start_from = get_int_parameter('start_from', flask.request, True, 0)
    except NotIntegerParameterException as e:
        return {'detail': '"%s" query parameter should be an integer number' % e.parameter}, 400
    except RequiredParameterNotFoundException as e:
        return {'detail': 'missing required query parameter "%s"' % e.parameter}, 400
    cursors = [{'partition': str(partition), 'offset': str(start_from)}]

    return __get_events(topic, cursors)


@measured('get_events_from_multiple_partitions')
@authenticate
def get_events_from_multiple_partitions(topic):

    # check if topic exists
    if not __topic_exists(topic):
        return {'detail': 'topic not found'}, 404

    # get cursors to start reading from
    cursors_str = flask.request.headers.get('x-nakadi-cursors')
    if not cursors_str:
        # if cursors are not specified - read from all partitions from the latest offset
        try:
            partitions_offsets = __get_partitions_offsets(topic)
        except:
            return {'detail': 'Not Available'}, 503
        cursors = [{
                       'partition': offset['partition_id'],
                       'offset': offset['newest_available_offset']
                   } for offset in partitions_offsets]
    else:
        try:
            cursors = get_cursors(cursors_str)
        except WrongCursorsFormatException:
            return {'detail': '"x-nakadi-cursors" header has wrong format'}, 400

    return __get_events(topic, cursors)


def __get_events(topic, cursors):

    # get and check parameters
    stream_opts = {}
    try:
        stream_opts['batch_limit'] = get_int_parameter('batch_limit', flask.request, False, 1)
        stream_opts['batch_flush_timeout'] = get_int_parameter('batch_flush_timeout', flask.request, False, 0)
        stream_opts['batch_keep_alive_limit'] = get_int_parameter('batch_keep_alive_limit', flask.request, False, -1)
        stream_opts['stream_limit'] = get_int_parameter('stream_limit', flask.request, False, 0)
        stream_opts['stream_timeout'] = get_int_parameter('stream_timeout', flask.request, False, 0)
    except NotIntegerParameterException as e:
        return {'detail': '"%s" query parameter should be an integer number' % e.parameter}, 400
    except RequiredParameterNotFoundException as e:
        return {'detail': 'missing required query parameter "%s"' % e.parameter}, 400

    # check that partitions exist
    for cursor in cursors:
        if not __partition_exists(topic, int(cursor['partition'])):
            return {'detail': 'partition not found'}, 404

    # returning generator in response will create a stream
    stream_generator = event_stream.create_stream_generator(kafka_client_pool, topic, cursors, stream_opts, __get_uid())
    return flask.Response(stream_generator, mimetype = 'text/plain', status = 200)


def __get_uid():
    return flask.request.token_info.get("uid")


@measured('post_event')
@authenticate
def post_event(topic):
    return __push_events_to_kafka(topic, [flask.request.json])


@measured('post_events')
@authenticate
def post_events(topic):
    call_start = datetime.datetime.now()
    encoding = flask.request.headers.get('Content-Encoding')
    if encoding == 'gzip':
        try:
            logging.info('Received compressed body. Uncompressing...')
            fake_file = BytesIO(flask.request.data)
            uncompressed = gzip.GzipFile(fileobj=fake_file, mode='r')
            json_bytes = uncompressed.read()
            json_data = flask.json.loads(json_bytes)
        except:
            return {'detail': 'Body decompression failed'}, 422
    else:
        json_data = flask.request.json

    ms_elapsed = monitoring.stop_time_measure(call_start)

    logging.info('Received batch of %s events', len(json_data))
    logging.info('[#DECMP_DESRL_TIME] Time spent on uncompression/deserialization: %s ms', ms_elapsed)
    return __push_events_to_kafka(topic, json_data)


def __push_events_to_kafka(topic, events):

    call_start = datetime.datetime.now()

    if __get_uid() not in config.UIDS_TO_POST_EVENT:
        logging.info('[#OAUTH_401] Received uuid is not valid for posting: %s', flask.request.token_info.get("uid"))
        return {'detail': 'Not Authorized. You are not allowed to use this endpoint'}, 401

    if not __topic_exists(topic):
        return {'detail': 'Topic does not exist'}, 422

    failed = 0
    for event in events:
        if 'partitioning_key' in event:
            key = event['partitioning_key']
        else:
            key = event['ordering_key']

        try:
            retry_if_failed(__produce_kafka_message, topic.encode('utf-8'), key.encode('utf-8'), json.dumps(event).encode('utf-8'))
        except:
            failed += 1

    ms_elapsed = monitoring.stop_time_measure(call_start)
    logging.info('[#KAFKA_PUSH_TIME] Time spent total for pushing to kafka: %s ms', ms_elapsed)

    metrics_writer.log_events(__get_uid(), topic, "-", len(events) - failed, 0)

    if failed > 0:
        return {'detail': 'Failed to write %s event(s) to kafka' % failed}, 503
    else:
        return {}, 201


def __produce_kafka_message(client, topic, key, event):
    partition_ids = client.get_partition_ids_for_topic(topic)
    key_stripped = key.decode("utf-8").upper().strip()
    key_hash = string_hashcode(key_stripped)
    partition_to_use = key_hash % len(partition_ids)
    logging.info("Using partition %s for key %s (stripped: %s, hash: %s)", partition_to_use, key, key_stripped, key_hash)

    producer = Producer(client)
    producer.send_messages(topic, partition_to_use, event)


@measured('get_metrics')
@authenticate
def get_metrics():
    metrics = {
        'last15min': {
            'endpoints': metrics_writer.aggregate_endpoints_stats(15),
            'events': metrics_writer.aggregate_consumption_stats(15)
        },
        'last5min': {
            'endpoints': metrics_writer.aggregate_endpoints_stats(5),
            'events': metrics_writer.aggregate_consumption_stats(5)
        },
        'last1min': {
            'endpoints': metrics_writer.aggregate_endpoints_stats(1),
            'events': metrics_writer.aggregate_consumption_stats(1)
        }
    }
    return metrics, 200


@measured('post_subscription')
@authenticate
def post_subscription():
    return not_implemented()


@measured('get_subscriptions')
@authenticate
def get_subscriptions():
    return not_implemented()


@measured('delete_subscription')
@authenticate
@measured
def delete_subscription(subscription_id):
    return not_implemented()


@measured('post_event_to_partition')
@authenticate
def post_event_to_partition(topic, partition):
    return not_implemented()


@measured('get_subscription')
@authenticate
def get_subscription(subscription_id):
    return not_implemented()


@measured('get_subscription_clients')
@authenticate
def get_subscription_clients(subscription_id):
    return not_implemented()


@measured('post_subscription_client')
@authenticate
def post_subscription_client(subscription_id):
    return not_implemented()


@measured('get_client')
@authenticate
def get_client(subscription_id, client_id):
    return not_implemented()


@measured('get_client_cursors')
@authenticate
def get_client_cursors(subscription_id, client_id):
    return not_implemented()


@measured('commit_cursor')
@authenticate
def commit_cursor(subscription_id, client_id):
    return not_implemented()


@measured('stream_for_client')
@authenticate
def stream_for_client(subscription_id, client_id):
    return not_implemented()


def not_implemented():
    return {'detail': 'Not Implemented'}, 501


# monkey patch KafkaConsumer to allow providing client as parameter
kafka_consumer_patch.monkey_patch_kafka_consumer()

# init logging
logging.basicConfig(level=logging.INFO)
logging.getLogger('kafka').setLevel(logging.ERROR)
logging.info('Starting aruha-event-store')

# create kafka clients pool
logging.info('Kafka broker list: %s' % config.KAFKA_BROKER)
kafka_client_pool = kafka_pool.KafkaClientPool(config.KAFKA_BROKER, config.KAFKA_CLIENTS_INIT_POOL_SIZE, config.KAFKA_CLIENTS_MAX_POOL_SIZE)

# create connexion application
conn_app = connexion.App(__name__, port=config.ARUHA_LISTEN_PORT, debug=True)
conn_app.add_api('swagger.yaml')

# expose flask application so that it can be run in external container
application=conn_app.app


# we map it with route-decorator because we don't want to have this endpoint in our API description schema
@conn_app.route('/health', methods=['GET'])
def health():
    return 'OK', 200
