"""ADES worker: lease factor slices from the controller, score them, report back.

    python -m assay.ades.worker --controller https://assay.qfinverse.com \\
        --key-file /etc/assay/worker.key --panel /var/lib/assay/panel --name w19-1

A worker is stateless apart from its local copy of the QFinVerse panel (pushed by
the controller host) and the spec it last loaded. It only makes outbound HTTPS
calls; it opens no port. Run one process per core (systemd ``assay-worker@N``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import threading
import time
import urllib.error
import urllib.request

from assay.ades import slice as sl

log = logging.getLogger("assay.ades.worker")

__all__ = ["HttpTransport", "Worker", "main"]


class HttpTransport:
    """``post(path, body) -> (status, json | None)`` against the controller."""

    def __init__(self, base: str, key: str, timeout: float = 60.0):
        self.base, self.key, self.timeout = base.rstrip("/"), key, timeout

    def post(self, path: str, body: dict) -> tuple[int, dict | None]:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None


class Worker:
    def __init__(self, transport, store, name: str):
        self.t, self.store, self.name = transport, store, name
        self.data: sl.SpecData | None = None
        self.data_version = None

    def _info(self) -> dict:
        return {"host": socket.gethostname(), "pid": os.getpid(), "cpus": os.cpu_count(),
                "lease_token_supported": True}

    def run_once(self) -> bool:
        """Lease and finish one task; ``False`` when there was nothing to do."""
        if hasattr(self.store, "refresh"):
            self.store.refresh()
        versions = list(self.store.versions().values()) if hasattr(self.store, 'versions') else [self.store.snapshot_id]
        status, task = self.t.post("/v1/ades/lease", {"worker": self.name, "panel": self.store.snapshot_id,
                                                      "panels":versions, "info": self._info()})
        if status != 200 or not task:
            return False
        stop = threading.Event()
        beat = threading.Thread(target=self._beat, args=(task, stop), daemon=True)
        beat.start()
        t0 = time.perf_counter()
        finished = False
        try:
            spec = task["spec"]
            store = self.store.for_universe(spec['universe']) if hasattr(self.store,'for_universe') else self.store
            if store.snapshot_id != task['version']:
                self.t.post('/v1/ades/release',dict(task_id=task['task_id'],worker=self.name,lease_token=task.get('lease_token')))
                finished = True
                return True
            if self.data is None or self.data.key != spec["key"] or self.data_version != task["version"]:
                self.data = None                       # drop the old panel before loading the next
                self.data = sl.load_spec(store, spec)
                self.data_version = self.data.version
                log.info("loaded spec %s in %.1fs", spec["key"], self.data.loaded_s)
            if self.data_version != task['version']:
                self.t.post('/v1/ades/release',dict(task_id=task['task_id'],worker=self.name,lease_token=task.get('lease_token')))
                finished = True
                return True
            results = sl.score_items(self.data, task["items"])
            self.t.post("/v1/ades/complete", {"task_id": task["task_id"], "lease_token":task.get("lease_token"), "worker": self.name,
                                              "seconds": round(time.perf_counter() - t0, 3), "results": results})
            finished = True
        except Exception as exc:  # noqa: BLE001 — report and keep serving
            log.exception("task %s failed", task.get("task_id"))
            self.t.post("/v1/ades/fail", {"task_id": task["task_id"], "lease_token":task.get("lease_token"), "worker": self.name, "error": str(exc)[:500]})
            finished = True
        finally:
            stop.set()
            if not finished:     # stopped mid-task (SIGTERM on a deploy): hand it back
                self.t.post("/v1/ades/release", {"task_id": task["task_id"], "lease_token":task.get("lease_token"), "worker": self.name})
        return True

    def _beat(self, task: dict, stop: threading.Event) -> None:
        every = max(5.0, float(task.get("lease_s", 120)) / 3)
        while not stop.wait(every):
            self.t.post("/v1/ades/heartbeat", {"task_id": task["task_id"], "lease_token":task.get("lease_token"), "worker": self.name})

    def serve(self, idle_max: float = 15.0) -> None:
        idle = 1.0
        while True:
            try:
                busy = self.run_once()
            except Exception:  # noqa: BLE001 — controller unreachable: back off
                log.exception("lease failed")
                busy = False
            if busy:
                idle = 1.0
            else:
                time.sleep(idle)
                idle = min(idle_max, idle * 2)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="ADES worker")
    ap.add_argument("--controller", default=os.environ.get("ASSAY_CONTROLLER", "https://assay.qfinverse.com"))
    ap.add_argument("--key-file", default=os.environ.get("ASSAY_WORKER_KEY_FILE", "/etc/assay/worker.key"))
    ap.add_argument("--panel", default=os.environ.get("ASSAY_QFV_PANEL_DIR", "/var/lib/assay/panel"))
    ap.add_argument("--name", default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from assay.data.store import QFVPanelStore

    def _stop(signum, frame):            # unwinds run_once, whose finally releases the lease
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    key = open(a.key_file).read().strip()
    name = a.name or f"{socket.gethostname()}-{os.getpid()}"
    from assay.data.markets import MarketStores
    Worker(HttpTransport(a.controller, key), MarketStores.from_env(us=QFVPanelStore(a.panel)), name).serve()


if __name__ == "__main__":
    main()
