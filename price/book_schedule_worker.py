"""Execute only independently approved finite book schedules.

This worker is separate from the proposal-only source/model worker. It has no
model, source mutation, bank transfer, filing or remote service capability.
"""
import argparse
import json
import time
import signal
from .db import Database
from .service import Service
from .book_schedules import BookSchedules


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    engine=BookSchedules(Service(Database(args.db)))
    from .pipeline_health import WorkerHeartbeat
    heartbeat=WorkerHeartbeat(engine.db,worker_name='price-bookkeeping')
    def interrupt(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupt)
    heartbeat.record(force=True)
    try:
        while True:
            heartbeat.record()
            result=engine.run_once()
            if result.get('processed') or args.once:
                print(json.dumps({k:result[k] for k in ('processed','state','journal_count','blocked_schedules') if k in result}),flush=True)
            heartbeat.record(result)
            if args.once:break
            time.sleep(.25 if result.get('processed') else 5)
    except KeyboardInterrupt:
        return
    finally:
        heartbeat.stopped()


if __name__=='__main__':main()
