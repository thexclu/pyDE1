"""
Copyright © 2021 Jeff Kletsky. All Rights Reserved.

License for this software, part of the pyDE1 package, is granted under
GNU General Public License v3.0 only
SPDX-License-Identifier: GPL-3.0-only
"""

import asyncio
import atexit
import concurrent.futures
import logging
import multiprocessing
import multiprocessing.connection as mpc
import signal
import threading
import time
from typing import Callable

from pyDE1.api.outbound.mqtt import run_api_outbound
from pyDE1.api.inbound.http import run_api_inbound

import pyDE1.default_logger

from pyDE1.controller import run_controller

if __name__ == "__main__":

    multiprocessing.set_start_method('spawn')

    loop = asyncio.get_event_loop()
    loop.set_debug(True)

    # If the controller is going to move into its own process
    # this process needs to handle the arrival of signals

    logger = logging.getLogger('try_de1')

    # Might be able to use SimpleQueue here,
    # at least until the queue gets joined at exit
    log_queue = multiprocessing.Queue()

    pyDE1.default_logger.initialize_default_logger(log_queue)
    pyDE1.default_logger.set_some_logging_levels()

    async def signal_handler(signal: signal.Signals,
                             loop: asyncio.AbstractEventLoop):
        logger.info(f"{str(signal)} {multiprocessing.active_children()}")

    signals = (
        signal.SIGCHLD,
    )

    for sig in signals:
        loop.add_signal_handler(
            sig,
            lambda sig=sig: asyncio.create_task(signal_handler(sig, loop),
                                                name=str(sig)))

    async def bye_bye(signal: signal.Signals,
                      loop: asyncio.AbstractEventLoop):
        t0 = time.time()
        logger = logging.getLogger('Shutdown')
        logger.info(f"{str(signal)} SHUTDOWN INITIATED "
                    f"{multiprocessing.active_children()}")
        # logger.info("Terminate API processes")
        # for p in multiprocessing.active_children():
        #     logger.info(f"Terminating {p}")
        #     p.terminate()
        logger.info("Waiting for processes to terminate")
        again = True
        while again:
            t1 = time.time()
            alive_in = inbound_api_process.is_alive()
            alive_out = outbound_api_process.is_alive()
            # logger.info(ac := multiprocessing.active_children())
            ac = multiprocessing.active_children()
            await asyncio.sleep(0.1)
            again = len(ac) > 0 and (t1 - t0 < 5)
            if not again:
                logger.info(f"Elapsed: {t1 - t0:0.3f} sec")
                if (t1 - t0 >= 5):
                    print("timed out with active_children ac}")
                    for p in (ac):
                        try:
                            p.kill()
                        except AttributeError:
                            pass
        logger.info("Terminating logging")
        print("_terminate_logging.set()")
        _terminate_logging.set()
        print("loop.stop()")
        loop.stop()
        # print("loop.close()")
        # loop.close()

    signals = (
        # signal.SIGHUP,
        signal.SIGINT,
        signal.SIGQUIT,
        signal.SIGABRT,
        signal.SIGTERM,
    )

    for sig in signals:
        loop.add_signal_handler(
            sig,
            lambda sig=sig: asyncio.create_task(
                bye_bye(sig, loop),
                name=str(sig)))

    # These assume that the executor is threading
    _rotate_logfile = threading.Event()
    _terminate_logging = threading.Event()

    def request_logfile_rotation(sig, frame):
        logger.info("Request to rotate log received")
        _rotate_logfile.set()

    signal.signal(signal.SIGHUP, request_logfile_rotation)

    inbound_pipe_controller, inbound_pipe_server = multiprocessing.Pipe()

    # read, write, for simplex
    outbound_pipe_read, outbound_pipe_write = multiprocessing.Pipe(
        duplex=False)

    @atexit.register
    def kill_stragglers():
        print("kill_stragglers()")
        procs = multiprocessing.active_children()
        for p in procs:
            print(f"Killing {p}")
            p.kill()
        print("buh-bye!")

    # MQTT API
    outbound_api_process = multiprocessing.Process(
        target=run_api_outbound,
        args=(log_queue, outbound_pipe_read),
        name='OutboundAPI',
        daemon=False)
    outbound_api_process.start()

    # HTTP API
    inbound_api_process = multiprocessing.Process(
        target=run_api_inbound,
        args=(log_queue, inbound_pipe_server),
        name='InboundAPI',
        daemon=False)
    inbound_api_process.start()

    # Core logic
    controller_process = multiprocessing.Process(
        target = run_controller,
        args=(
            log_queue,
            inbound_pipe_controller,
            outbound_pipe_write,
        ),
        name="Controller",
        daemon=False
    )
    controller_process.start()

    #
    # Now that the other processes are running, define the log handler
    # this will eventually get moved out
    #

    import os

    LOG_DIRECTORY = '/tmp/log/pyDE1/'
    LOG_FILENAME = 'combined.log'


    def log_queue_reader_blocks(log_queue: multiprocessing.Queue,
                                terminate_logging_event: threading.Event,
                                rotate_log_event: threading.Event):
        if not os.path.exists(LOG_DIRECTORY):
            logger.error(
                "logfile_directory '{}' does not exist. Creating.".format(
                    os.path.realpath(LOG_DIRECTORY)
                )
            )
            # Will create intermediate directories
            # Will not use "mode" on intermediates
            os.makedirs(LOG_DIRECTORY)
        fq_logfile = os.path.join(LOG_DIRECTORY, LOG_FILENAME)
        while not terminate_logging_event.is_set():
            with open(file=fq_logfile, mode='a', buffering=1) as fh:
                logger.info(f"Opening log file")
                while not terminate_logging_event.is_set():
                    record = log_queue.get()
                    # LogRecord is what gets enqueued
                    # TODO: Use QueueListener to further filter?
                    fh.write(record.msg + "\n")
                    try:
                        log_queue.task_done()
                    except AttributeError:
                        # multiprocessing.Queue() does not have .task_done()
                        pass

                    if rotate_log_event.is_set():
                        # TODO: Can this be formatted?
                        fh.write(f"Rotating log file\n")
                        fh.flush()
                        fh.close()
                        rotate_log_event.clear()
                        break

    logging_tpe = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix='LogQueueReader')

    loop.run_in_executor(logging_tpe,
                         log_queue_reader_blocks,
                         log_queue, _terminate_logging, _rotate_logfile)

    loop.run_forever()
    print("after loop.run_forever()")
    # explicit TPE shutdown hangs
    # print("shutdown TPE")
    # logging_tpe.shutdown(cancel_futures=True)
    # print("after shutdown TPE")
    print(f"active_children: {multiprocessing.active_children()}")
    print("loop.close()")
    # loop.close() seems to be the source of a kill-related exit code
    loop.close()
    print("after loop.close()")

