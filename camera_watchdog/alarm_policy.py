"""Persistent alarm latch for confirmed fixed-reference Census changes."""
import json
import os
from pathlib import Path
import threading


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
                candidate = {**self.states, slug: changed}
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(candidate, indent=2))
                os.replace(temporary, self.path)
                self.states = candidate
            return changed, ready

    def get(self, slug):
        with self.lock:
            return self.states.get(slug, False)
