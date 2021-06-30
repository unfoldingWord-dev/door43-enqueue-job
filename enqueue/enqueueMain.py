# Adapted by RJH June 2018 from fork of https://github.com/lscsoft/webhook-queue (Public Domain / unlicense.org)
#   The main change was to add some vetting of the json payload before allowing the job to be queued.
#   Updated Sept 2018 to add callback service

# TODO: We don't currently have any way to clear the failed queue

# Python imports
from os import getenv, environ
import sys
from datetime import datetime, timedelta
import logging

# Library (PyPI) imports
from flask import Flask, request, jsonify
# NOTE: We use StrictRedis() because we don't need the backwards compatibility of Redis()
from redis import StrictRedis
from rq import Queue, Worker
from statsd import StatsClient # Graphite front-end
from boto3 import Session
from watchtower import CloudWatchLogHandler


# Local imports
from check_posted_payload import check_posted_payload, check_posted_callback_payload


OUR_NAME = 'Door43_Enqueue' # Used for logging
DJH_NAME = 'Door43_webhook' # Becomes the (perhaps prefixed) queue name (and graphite name) -- MUST match setup.py in door43-job-handler
DCJH_NAME = 'Door43_catalog_webhook'
CALLBACK_SUFFIX = '_callback'
DEV_PREFIX = 'dev-'

# NOTE: The following strings if not empty, MUST have a trailing slash but NOT a leading one.
#WEBHOOK_URL_SEGMENT = 'client/webhook/'
WEBHOOK_URL_SEGMENT = '' # Leaving this blank will cause the service to run at '/'
CALLBACK_URL_SEGMENT = WEBHOOK_URL_SEGMENT + 'tx-callback/'


# Look at relevant environment variables
prefix = getenv('QUEUE_PREFIX', '') # Gets (optional) QUEUE_PREFIX environment variable -- set to 'dev-' for development
prefixed_our_name = prefix + OUR_NAME
prefixed_djh_name = prefix + DJH_NAME
prefixed_dcjh_name = prefix + DCJH_NAME
echo_prodn_to_dev_flag = False


# NOTE: Large lexicons like UGL and UAHL seem to be the longest-running jobs
WEBHOOK_TIMEOUT = '900s' if prefix else '600s' # Then a running job (taken out of the queue) will be considered to have failed
    # NOTE: This is only the time until webhook.py returns after preprocessing and submitting the job
    #           -- the actual conversion jobs might still be running.
    # RJH: 480s fails on UHB 76,000+ link checks for my slow internet (took 361s)
    # RJH: 480s fails on UGNT 33,000+ link checks for my slow internet (took 596s)
CALLBACK_TIMEOUT = '1200s' if prefix else '600s' # Then a running callback job (taken out of the queue) will be considered to have failed
    # RJH: 480s fails on UGL upload for my slow internet (600s fails even on mini UGL upload!!!)

# Get the redis URL from the environment, otherwise use a local test instance
redis_hostname = getenv('REDIS_HOSTNAME', 'redis')
# Use this to detect test mode (coz logs will go into a separate AWS CloudWatch stream)
debug_mode_flag = 'gogs' not in redis_hostname # Typically set to 'redis'
test_string = " (TEST)" if debug_mode_flag else ""


# Setup logging
logger = logging.getLogger(prefixed_our_name)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
logger.addHandler(sh)
aws_access_key_id = environ['AWS_ACCESS_KEY_ID']
boto3_session = Session(aws_access_key_id=aws_access_key_id,
                        aws_secret_access_key=environ['AWS_SECRET_ACCESS_KEY'],
                        region_name='us-west-2')
test_mode_flag = getenv('TEST_MODE', '')
travis_flag = getenv('TRAVIS_BRANCH', '')
log_group_name = f"{'' if test_mode_flag or travis_flag else prefix}tX" \
                 f"{'_DEBUG' if debug_mode_flag else ''}" \
                 f"{'_TEST' if test_mode_flag else ''}" \
                 f"{'_TravisCI' if travis_flag else ''}"
watchtower_log_handler = CloudWatchLogHandler(boto3_session=boto3_session,
                                              log_group=log_group_name,
                                              stream_name=prefixed_our_name)
logger.addHandler(watchtower_log_handler)
# Enable DEBUG logging for dev- instances (but less logging for production)
logger.setLevel(logging.DEBUG if prefix else logging.INFO)
logger.debug(f"Logging to AWS CloudWatch group '{log_group_name}' using key '…{aws_access_key_id[-2:]}'.")


# Setup queue variables
QUEUE_NAME_SUFFIX = '' # Used to switch to a different queue, e.g., '_1'
if prefix not in ('', DEV_PREFIX):
    logger.critical(f"Unexpected prefix: '{prefix}' — expected '' or '{DEV_PREFIX}'")
if prefix: # don't use production queue
    djh_adjusted_webhook_queue_name = prefixed_djh_name + QUEUE_NAME_SUFFIX # Will become our main queue name
    djh_adjusted_callback_queue_name = prefixed_djh_name + CALLBACK_SUFFIX + QUEUE_NAME_SUFFIX
    djh_other_adjusted_queue_name = DJH_NAME + QUEUE_NAME_SUFFIX # The other queue name
    djh_other_adjusted_callback_queue_name = DJH_NAME + CALLBACK_SUFFIX + QUEUE_NAME_SUFFIX
    dcjh_adjusted_queue_name = prefixed_dcjh_name + QUEUE_NAME_SUFFIX # Will become the catalog handler queue name
else: # production code
    djh_adjusted_webhook_queue_name = DJH_NAME + QUEUE_NAME_SUFFIX # Will become our main queue name
    djh_adjusted_callback_queue_name = DJH_NAME + CALLBACK_SUFFIX + QUEUE_NAME_SUFFIX
    djh_other_adjusted_queue_name = DEV_PREFIX + djh_adjusted_webhook_queue_name # The other queue name
    djh_other_adjusted_callback_queue_name = DEV_PREFIX + djh_adjusted_callback_queue_name
    dcjh_adjusted_queue_name = DCJH_NAME + QUEUE_NAME_SUFFIX # Will become the catalog handler queue name
# NOTE: The prefixed version must also listen at a different port (specified in gunicorn run command)


prefix_string = f" ({prefix})" if prefix else ""
logger.info(f"enqueueMain.py{prefix_string}{test_string} running on Python v{sys.version}")


# Connect to Redis now so it fails at import time if no Redis instance available
logger.info(f"redis_hostname is '{redis_hostname}'")
logger.debug(f"{prefixed_our_name} connecting to Redis…")
redis_connection = StrictRedis(host=redis_hostname)
logger.debug("Getting total worker count in order to verify working Redis connection…")
total_rq_worker_count = Worker.count(connection=redis_connection)
logger.debug(f"Total rq workers = {total_rq_worker_count}")


# Get the Graphite URL from the environment, otherwise use a local test instance
graphite_url = getenv('GRAPHITE_HOSTNAME', 'localhost')
logger.info(f"graphite_url is '{graphite_url}'")
stats_prefix = f"door43.{'dev' if prefix else 'prod'}.enqueue-job"
stats_client = StatsClient(host=graphite_url, port=8125, prefix=stats_prefix)


app = Flask(__name__)
# Not sure that we need this Flask logging
# app.logger.addHandler(watchtower_log_handler)
# logging.getLogger('werkzeug').addHandler(watchtower_log_handler)
logger.info(f"{prefixed_djh_name}, callback and {prefixed_dcjh_name} are up and ready to go")


def handle_failed_queue(queue_name:str) -> int:
    """
    Go through the failed queue, and see how many entries originated from our queue.

    Of those, permanently delete any that are older than two weeks old.
    """
    failed_queue = Queue('failed', connection=redis_connection)
    len_failed_queue = len(failed_queue)
    if len_failed_queue:
        logger.debug(f"There are {len_failed_queue} total jobs in failed queue")

    len_failed_queue = 0
    for failed_job in failed_queue.jobs.copy():
        if failed_job.origin == queue_name:
            failed_duration = datetime.utcnow() - failed_job.enqueued_at
            if failed_duration >= timedelta(weeks=2):
                logger.info(f"Deleting expired '{queue_name}' failed job from {failed_job.enqueued_at}")
                failed_job.delete() # .cancel() doesn't delete the Redis hash
            else:
                len_failed_queue += 1

    if len_failed_queue:
        logger.info(f"Have {len_failed_queue} of our jobs in failed queue")
    return len_failed_queue
# end of handle_failed_queue function


# This is the main workhorse part of this code
#   rq automatically returns a "Method Not Allowed" error for a GET, etc.
@app.route('/'+WEBHOOK_URL_SEGMENT, methods=['POST'])
def job_receiver():
    """
    Accepts POST requests and checks the (json) payload

    Queues the approved jobs at redis instance at global redis_hostname:6379.
    Queue name is djh_adjusted_webhook_queue_name and dcjh_adjusted_queue_name(may have been prefixed).
    """
    #assert request.method == 'POST'
    stats_client.incr('webhook.posts.attempted')
    logger.info(f"WEBHOOK received by {prefixed_our_name}: {request}")
    # NOTE: 'request' above typically displays something like "<Request 'http://git.door43.org/' [POST]>"

    djh_queue = Queue(djh_adjusted_webhook_queue_name, connection=redis_connection)
    dcjh_queue = Queue(dcjh_adjusted_queue_name, connection=redis_connection)

    # Collect and log some helpful information
    len_djh_queue = len(djh_queue) # Should normally sit at zero here
    stats_client.gauge('webhook.queue.length.current', len_djh_queue)
    len_djh_failed_queue = handle_failed_queue(djh_adjusted_webhook_queue_name)
    stats_client.gauge('webhook.queue.length.failed', len_djh_failed_queue)
    len_dcjh_queue = len(dcjh_queue) # Should normally sit at zero here
    stats_client.gauge('webhook.queue.length.current', len_dcjh_queue)
    len_dcjh_failed_queue = handle_failed_queue(dcjh_adjusted_queue_name)
    stats_client.gauge('webhook.queue.length.failed', len_dcjh_failed_queue)

    # Find out how many workers we have
    total_worker_count = Worker.count(connection=redis_connection)
    logger.debug(f"Total rq workers = {total_worker_count}")
    djh_queue_worker_count = Worker.count(queue=djh_queue)
    logger.debug(f"Our {djh_adjusted_webhook_queue_name} queue workers = {djh_queue_worker_count}")
    stats_client.gauge('webhook.workers.available', djh_queue_worker_count)
    if djh_queue_worker_count < 1:
        logger.critical(f'{prefixed_djh_name} has no job handler workers running!')
        # Go ahead and queue the job anyway for when a worker is restarted

    dcjh_queue_worker_count = Worker.count(queue=dcjh_queue)
    logger.debug(f"Our {dcjh_adjusted_queue_name} queue workers = {dcjh_queue_worker_count}")
    stats_client.gauge('webhook.workers.available', dcjh_queue_worker_count)
    if dcjh_queue_worker_count < 1:
        logger.critical(f'{prefixed_dcjh_name} has no job handler workers running!')
        # Go ahead and queue the job anyway for when a worker is restarted

    response_ok_flag, response_dict = check_posted_payload(request, logger)
    # response_dict is json payload if successful, else error info
    if response_ok_flag:
        logger.debug(f"{prefixed_our_name} queuing good payload…")

        # Check for special switch to echo production requests to dev- chain
        global echo_prodn_to_dev_flag
        if not prefix: # Only apply to production chain
            try:
                repo_name = response_dict['repository']['full_name']
            except (KeyError, AttributeError):
                repo_name = None
            if repo_name == 'tx-manager-test-data/echo_prodn_to_dev_on':
                echo_prodn_to_dev_flag = True
                logger.info("TURNED ON 'echo_prodn_to_dev_flag'!\n")
                stats_client.incr('webhook.posts.succeeded')
                return jsonify({'success': True, 'status': 'echo ON'})
            if repo_name == 'tx-manager-test-data/echo_prodn_to_dev_off':
                echo_prodn_to_dev_flag = False
                logger.info("Turned off 'echo_prodn_to_dev_flag'.\n")
                stats_client.incr('webhook.posts.succeeded')
                return jsonify({'success': True, 'status': 'echo off'})

        # Add our fields
        response_dict['door43_webhook_retry_count'] = 0 # In case we want to retry failed jobs
        response_dict['door43_webhook_received_at'] = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ') # Used to calculate total elapsed time

        # NOTE: No ttl specified on the next line -- this seems to cause unrun jobs to be just silently dropped
        #           (For now at least, we prefer them to just stay in the queue if they're not getting processed.)
        #       The timeout value determines the max run time of the worker once the job is accessed
        djh_queue.enqueue('webhook.job', response_dict, job_timeout=WEBHOOK_TIMEOUT) # A function named webhook.job will be called by the worker
        dcjh_queue.enqueue('webhook.job', response_dict, job_timeout=WEBHOOK_TIMEOUT) # A function named webhook.job will be called by the worker
        # NOTE: The above line can return a result from the webhook.job function. (By default, the result remains available for 500s.)

        # See if we want to echo this job to the dev- queue (used for dev- code testing)
        other_queue = Queue(djh_other_adjusted_queue_name, connection=redis_connection)
        if echo_prodn_to_dev_flag: # Should only be set on production chain (so repo_name should be set)
            if repo_name == 'acceptance_test/test':
                logger.info(f"Not echoing '{repo_name}' to {djh_other_adjusted_queue_name} queue.")
            else: # for all others
                logger.info(f"ALSO ECHOING JOB to {djh_other_adjusted_queue_name} queue…")
                logger.info("  (Use https://git.door43.org/tx-manager-test-data/echo_prodn_to_dev_off/settings/hooks/44079 to turn this off.)")
                response_dict['echoed_from_production'] = True
                other_queue.enqueue('webhook.job', response_dict, job_timeout=WEBHOOK_TIMEOUT) # A function named webhook.job will be called by the worker

        # Find out who our workers are
        #workers = Worker.all(connection=redis_connection) # Returns the actual worker objects
        #logger.debug(f"Total rq workers ({len(workers)}): {workers}")
        #djh_queue_workers = Worker.all(queue=djh_queue)
        #logger.debug(f"Our {djh_adjusted_webhook_queue_name} queue workers ({len(djh_queue_workers)}): {djh_queue_workers}")

        len_djh_queue = len(djh_queue) # Update
        len_dcjh_queue = len(dcjh_queue) # Update
        logger.info(f"{prefixed_djh_name} queued valid job to {djh_adjusted_webhook_queue_name} queue " \
                    f"({len_djh_queue} jobs now " \
                        f"for {Worker.count(queue=djh_queue)} workers, " \
                    f"({len_dcjh_queue} jobs now " \
                        f"for {Worker.count(queue=dcjh_queue)} workers, " \
                    f"{len(other_queue)} jobs in {djh_other_adjusted_queue_name} queue " \
                        f"for {Worker.count(queue=other_queue)} workers, " \
                    f"{len_djh_failed_queue} failed jobs) at {datetime.utcnow()}, " \
                    f"{len_dcjh_failed_queue} failed jobs) at {datetime.utcnow()}\n")

        webhook_return_dict = {'success': True,
                               'status': 'queued',
                               'queue_name': djh_adjusted_webhook_queue_name,
                               'door43_job_queued_at': datetime.utcnow()}
        if echo_prodn_to_dev_flag:
            webhook_return_dict['echoed_queue_name'] = djh_other_adjusted_queue_name
        stats_client.incr('webhook.posts.succeeded')
        return jsonify(webhook_return_dict)
    #else:
    stats_client.incr('webhook.posts.invalid')
    response_dict['status'] = 'invalid'
    try: detail = request.headers['X-Gitea-Event']
    except KeyError: detail = "No X-Gitea-Event"
    logger.error(f"{prefixed_our_name} ignored invalid '{detail}' payload; responding with {response_dict}\n")
    return jsonify(response_dict), 400
# end of job_receiver()


@app.route('/'+CALLBACK_URL_SEGMENT, methods=['POST'])
def callback_receiver():
    """
    Accepts POST requests and checks the (json) payload

    Queues the approved jobs at redis instance at global redis_hostname:6379.
    Queue name is djh_adjusted_callback_queue_name (may have been prefixed).
    """
    #assert request.method == 'POST'
    stats_client.incr('callback.posts.attempted')
    logger.info(f"CALLBACK received by {prefixed_djh_name}: {request}")

    # Collect (and log) some helpful information
    djh_queue = Queue(djh_adjusted_callback_queue_name, connection=redis_connection)
    len_djh_queue = len(djh_queue) # Should normally sit at zero here
    stats_client.gauge('callback.queue.length.current', len_djh_queue)
    len_djh_failed_queue = handle_failed_queue(djh_adjusted_callback_queue_name)
    stats_client.gauge('callback.queue.length.failed', len_djh_failed_queue)

    response_ok_flag, response_dict = check_posted_callback_payload(request, logger)
    # response_dict is json payload if successful, else error info
    if response_ok_flag:
        logger.debug(f"{prefixed_our_name} queuing good callback…")

        # Add our fields
        response_dict['door43_callback_retry_count'] = 0

        # NOTE: No ttl specified on the next line -- this seems to cause unrun jobs to be just silently dropped
        #           (For now at least, we prefer them to just stay in the queue if they're not getting processed.)
        #       The timeout value determines the max run time of the worker once the job is accessed
        djh_queue.enqueue('callback.job', response_dict, job_timeout=CALLBACK_TIMEOUT) # A function named callback.job will be called by the worker
        # NOTE: The above line can return a result from the callback.job function. (By default, the result remains available for 500s.)

        # Find out who our workers are
        #workers = Worker.all(connection=redis_connection) # Returns the actual worker objects
        #logger.debug(f"Total rq workers ({len(workers)}): {workers}")
        #djh_queue_workers = Worker.all(queue=djh_queue)
        #logger.debug(f"Our {djh_adjusted_callback_queue_name} queue workers ({len(djh_queue_workers)}): {djh_queue_workers}")

        # Find out how many workers we have
        #worker_count = Worker.count(connection=redis_connection)
        #logger.debug(f"Total rq workers = {worker_count}")
        #djh_queue_worker_count = Worker.count(queue=djh_queue)
        #logger.debug(f"Our {djh_adjusted_callback_queue_name} queue workers = {djh_queue_worker_count}")

        len_djh_queue = len(djh_queue) # Update
        len_dcjh_queue = len(dcjh_queue) # Update
        other_callback_queue = Queue(djh_other_adjusted_callback_queue_name, connection=redis_connection)
        logger.info(f"{prefixed_djh_name} queued valid callback job to {djh_adjusted_callback_queue_name} queue " \
                    f"({len_djh_queue} jobs now " \
                        f"for {Worker.count(queue=djh_queue)} workers, " \
                    f"({len_dcjh_queue} jobs now " \
                        f"for {Worker.count(queue=dcjh_queue)} workers, " \
                    f"{len(other_callback_queue)} jobs in {djh_other_adjusted_callback_queue_name} queue " \
                        f"for {Worker.count(queue=other_callback_queue)} workers, " \
                    f"{len_djh_failed_queue} failed jobs) at {datetime.utcnow()}, " \
                    f"{len_dcjh_failed_queue} failed jobs) at {datetime.utcnow()}\n")

        callback_return_dict = {'success': True,
                                'status': 'queued',
                                'queue_name': djh_adjusted_callback_queue_name,
                                'door43_callback_queued_at': datetime.utcnow()}
        stats_client.incr('callback.posts.succeeded')
        return jsonify(callback_return_dict)
    #else:
    stats_client.incr('callback.posts.invalid')
    response_dict['status'] = 'invalid'
    logger.error(f"{prefixed_our_name} ignored invalid callback payload; responding with {response_dict}\n")
    return jsonify(response_dict), 400
# end of callback_receiver()


if __name__ == '__main__':
    app.run()
