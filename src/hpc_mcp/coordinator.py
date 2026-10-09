"""Detached local coordinator, controlled through persistent state rather than stdio."""

import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sys
import time

from .config import Cluster
from .history import History, now
from .jobs import JobService
from .monitor import MonitorService, settings_from, state_lock
from .monitor_engine import MonitorEngine


def main():
    history = History(sys.argv[1])
    instance_id = sys.argv[2]
    service = MonitorService(JobService({}, history.root))
    logger = logging.getLogger('hpc-mcp.monitor')
    logger.setLevel(logging.INFO)
    path = history.root / 'monitor.log'
    if path.is_symlink():
        service.runtime_update(instance_id, state='failed', error='monitor log cannot be a symlink')
        raise SystemExit(1)
    handler = RotatingFileHandler(path, maxBytes=1048576, backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    stopped = [False]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.__setitem__(0, True))
    try:
        with state_lock(history, 'monitor-daemon.lock', blocking=False):
            runtime = service.runtime()
            if runtime.get('instance_id') != instance_id:
                return
            clusters = {name: Cluster(**values) for name, values in runtime['clusters'].items()}
            service = MonitorService(JobService(clusters, history.root))
            settings = settings_from(runtime['settings'])
            engine = MonitorEngine(service, settings, stopping=lambda: stopped[0])
            service.runtime_update(instance_id, state='running', pid=os.getpid(), heartbeat_at=now())
            logger.info('Coordinator %s started; %d cluster configurations', instance_id, len(clusters))
            while not stopped[0]:
                runtime = service.runtime()
                if runtime.get('instance_id') != instance_id or runtime.get('stop_requested'):
                    break
                service.runtime_update(instance_id, heartbeat_at=now())
                try:
                    summary = engine.tick()
                    service.runtime_update(instance_id, last_cycle=summary, last_error=None, heartbeat_at=now())
                except Exception as error:
                    logger.exception('Coordinator cycle failed')
                    service.runtime_update(instance_id, last_error=str(error)[:4000], heartbeat_at=now())
                    # A failing maintenance action must not create a hot loop.
                    deadline = time.monotonic() + settings.retry_initial_seconds
                    while time.monotonic() < deadline and not stopped[0] and not service.runtime().get('stop_requested'):
                        time.sleep(0.5)
                time.sleep(0.5)
            service.runtime_update(instance_id, state='stopped', stopped_at=now(), heartbeat_at=now())
            logger.info('Coordinator %s stopped; existing sync workers are independent', instance_id)
    except Exception as error:
        logger.exception('Coordinator failed')
        service.runtime_update(instance_id, state='failed', error=str(error)[:4000])
        raise SystemExit(1)
    finally:
        handler.close()


if __name__ == '__main__':
    main()
