"""Background worker: embeds pending document versions, keeps the knowledge graph in step
with company records, suggests document relationships for review, and applies chat retention.

Run as its own process (`python -m abteilo.worker`). Several copies are safe:
indexing writes are conditional on the version still being pending, graph
projection is idempotent and serialised per company by a row lock, extraction
candidates are unique per document version, and retention purges are idempotent
deletes of expired rows.
"""

import logging
import signal
import threading
import time

from abteilo.graph_extraction import extract_all
from abteilo.graph_projection import project_all
from abteilo.indexer import index_all
from abteilo.retention import purge_all

INDEX_INTERVAL_SECONDS = 20
RETENTION_INTERVAL_SECONDS = 3600
log = logging.getLogger("abteilo.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    next_retention = 0.0
    while not stop.is_set():
        try:
            run = index_all()
            if run.indexed or run.failed or run.retried:
                log.info('{"event":"index_run","indexed":%d,"failed":%d,"retried":%d}', run.indexed, run.failed, run.retried)
        except Exception:
            log.exception('{"event":"index_failed"}')
        try:
            graph = project_all()
            if graph.changes:
                log.info('{"event":"graph_projection","entities_changed":%d,"edges_added":%d,"edges_ended":%d}',
                         graph.entities_changed, graph.edges_added, graph.edges_ended)
            extraction = extract_all()
            if extraction.done or extraction.failed:
                log.info('{"event":"graph_extraction","versions":%d,"failed":%d,"candidates":%d}',
                         extraction.done, extraction.failed, extraction.candidates)
        except Exception:
            log.exception('{"event":"graph_failed"}')
        if time.monotonic() >= next_retention:
            try:
                results = purge_all()
                log.info('{"event":"retention_run","companies":%d,"conversations_deleted":%d}',
                         len(results), sum(r.conversations_deleted for r in results))
            except Exception:
                log.exception('{"event":"retention_failed"}')
            next_retention = time.monotonic() + RETENTION_INTERVAL_SECONDS
        stop.wait(INDEX_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
