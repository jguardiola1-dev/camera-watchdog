"""Persistent alarm latch for confirmed fixed-reference Census changes."""
import json
import os
from pathlib import Path
import threading
import time


class AlarmLatches:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.states = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.states, dict) or any(type(v) is not bool for v in self.states.values()):
            raise ValueError('Invalid persisted alarm state')

    def update(self, slug, result):
        with self.lock:
            previous = self.states.get(slug, False)
            changed, ready = previous, previous
            if result.get('comparison_mode') == 'day_night_references':
                if result.get('reliable') and result.get('status') == 'stable':
                    changed, ready = False, True
                elif (result.get('reliable') and result.get('scene_change')
                      and result.get('observed_scene_loss')):
                    changed, ready = True, True
            if changed != previous:
                self._store(slug, changed)
            return changed, ready

    def _store(self, slug, changed):
        candidate = {**self.states, slug: changed}
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(candidate, indent=2))
        os.replace(temporary, self.path)
        self.states = candidate

    def get(self, slug):
        with self.lock:
            return self.states.get(slug, False)


class SignalLossMonitor(AlarmLatches):
    """Confirm sustained capture failures; retain confirmed alarms across restarts."""

    def __init__(self, path):
        super().__init__(path)
        self.failures = {}
        self.validated = set()

    def failure(self, slug, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            previous = self.failures.get(slug)
            self.failures[slug] = (previous[0], previous[1] + 1) if previous else (now, 1)
            self.validated.discard(slug)

    def success(self, slug):
        with self.lock:
            if self.states.get(slug, False):
                self._store(slug, False)
            self.failures.pop(slug, None)
            self.validated.add(slug)

    def reset(self, slug):
        with self.lock:
            self.success(slug)
            self.validated.discard(slug)

    def status(self, slug, timeout=120, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            since, count = self.failures.get(slug, (now, 0))
            elapsed = max(0, now - since)
            confirmed = self.states.get(slug, False)
            if not confirmed and count >= 2 and elapsed >= timeout:
                self._store(slug, True)
                confirmed = True
            return dict(confirmed=confirmed, pending=bool(count) and not confirmed,
                        failures=count, elapsed_seconds=round(elapsed), timeout_seconds=timeout,
                        capture_validated=slug in self.validated)
