"""Detached sync worker invoked only for a stored operation."""

import sys
import os
from .config import Cluster
from .jobs import JobService
from .history import History
from .operations import operation_get, operation_update


def main():
    history = History(sys.argv[1])
    operation_id = sys.argv[2]
    operation = operation_get(history, operation_id)
    try:
        # Parent releases the short reservation lock just after launching this process.
        import time
        jobs = JobService({operation["cluster"]["name"]: Cluster(**operation["cluster"])}, history.root)
        operation_update(history, operation_id, state="running", pid=os.getpid())
        for attempt in range(50):
            try:
                result = jobs.job_sync(operation["run_id"], _operation_id=operation_id, **operation["options"])
                break
            except ValueError as error:
                if "another submit/recover/sync" not in str(error) or attempt == 49:
                    raise
                time.sleep(0.1)
        operation_update(history, operation_id, state="succeeded" if result["ok"] else "failed", result=result,
                         progress=result.get("run", {}).get("sync_progress", {}))
    except Exception as error:
        operation_update(history, operation_id, state="failed", error=str(error))


if __name__ == "__main__":
    main()
