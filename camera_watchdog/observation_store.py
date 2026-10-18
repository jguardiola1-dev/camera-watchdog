"""Bounded, credential-free collection of diagnostics and sampled JPEGs."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from zoneinfo import ZoneInfo


class ObservationStore:
    def __init__(self, root, max_bytes=512 * 1024 * 1024, retention_days=7,
                 snapshot_seconds=60, min_free_bytes=1024 * 1024 * 1024):
        if Path(root).is_symlink():
            raise ValueError("Unsafe observation root")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.max_age = retention_days * 86400
        self.snapshot_seconds = snapshot_seconds
        self.min_free_bytes = min_free_bytes
        self._last_snapshot = {}
        self._lock = threading.RLock()
        self._files = {}
        self._bytes = 0
        self._last_scan = 0
        self._scan(time.time())

    def _managed(self, path):
        return (not path.is_symlink() and path.resolve().is_relative_to(self.root)
                and path.parent.parent == self.root and not path.parent.is_symlink()
                and path.parent.name.startswith("hour-")
                and (path.name.startswith("sample-") and path.suffix == ".jpg"
                     or path.name.startswith("metrics-") and path.suffix == ".jsonl"))

    def _scan(self, now):
        self._files = {}
        for folder in self.root.glob("hour-*"):
            if folder.is_symlink() or not folder.is_dir():
                continue
            for path in folder.iterdir():
                if path.is_file() and self._managed(path):
                    stat = path.stat()
                    self._files[path] = (stat.st_mtime, stat.st_size)
        self._bytes = sum(size for _, size in self._files.values())
        self._last_scan = now

    def _prune(self, now, needed=0, protect=None):
        if now - self._last_scan > 300:
            self._scan(now)
        for path, (modified, size) in sorted(self._files.items(), key=lambda item: item[1][0]):
            if path == protect:
                continue
            if modified < now - self.max_age or self._bytes + needed > self.max_bytes:
                if not self._managed(path):
                    continue
                path.unlink(missing_ok=True)
                self._bytes -= size
                del self._files[path]
                try:
                    path.parent.rmdir()
                except OSError:
                    pass

    def _track(self, path, now):
        old_size = self._files.get(path, (0, 0))[1]
        size = path.stat().st_size
        self._files[path] = (now, size)
        self._bytes += size - old_size

    def stats(self):
        with self._lock:
            if time.time() - self._last_scan > 300:
                self._scan(time.time())
            images = [size for p, (_, size) in self._files.items() if p.suffix == '.jpg']
            return dict(image_count=len(images), image_bytes=sum(images),
                        diagnostic_bytes=self._bytes - sum(images), max_bytes=self.max_bytes,
                        retention_days=self.max_age / 86400)

    def clear_images(self):
        result = self._clear_files('.jpg')
        return dict(images_removed=result['files_removed'], bytes_freed=result['bytes_freed'])

    def clear_diagnostics(self):
        return self._clear_files('.jsonl')

    def _clear_files(self, suffix):
        """Remove one generated history type only, serialized with writers."""
        with self._lock:
            self._scan(time.time())
            removed, freed = 0, 0
            try:
                for path, (_, size) in list(self._files.items()):
                    if path.suffix != suffix or not self._managed(path):
                        continue
                    path.unlink(missing_ok=True)
                    removed += 1
                    freed += size
            finally:
                if suffix == '.jpg':
                    self._last_snapshot.clear()
                self._scan(time.time())
            return dict(files_removed=removed, bytes_freed=freed)

    def record(self, slug, metrics, image_path=None, now=None, extra_images=None,
               images_enabled=True, diagnostics_enabled=True):
        now = time.time() if now is None else float(now)
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(slug))[:80] or "camera"
        instant = datetime.fromtimestamp(now, timezone.utc)
        folder = self.root / instant.strftime("hour-%Y%m%dT%H")
        if folder.is_symlink():
            raise ValueError("Unsafe observation directory")
        journal = folder / f"metrics-{slug}.jsonl"
        with self._lock:
            self._prune(now)
            if diagnostics_enabled:
                folder.mkdir(parents=True, exist_ok=True)
            if journal.is_symlink():
                raise ValueError("Unsafe journal")
            segment_limit = max(256, min(1024 * 1024, self.max_bytes // 8))
            if journal.exists() and journal.stat().st_size >= segment_limit:
                journal = folder / f"metrics-{slug}-{instant.strftime('%M%S%f')}.jsonl"
                if journal.is_symlink():
                    raise ValueError("Unsafe journal segment")
            record = dict(metrics)
            record.update(timestamp_utc=instant.isoformat(), camera=slug,
                          mode=metrics.get("mode", "collection_only"),
                          alarms_published=bool(metrics.get("alarms_published", False)),
                          image=None, evidence={})
            try:
                record["timestamp_local"] = instant.astimezone(ZoneInfo("Europe/Madrid")).isoformat()
            except Exception:
                record["timestamp_local"] = instant.isoformat()
            image = None
            if (images_enabled and image_path is not None
                    and now - self._last_snapshot.get(slug, float("-inf")) >= self.snapshot_seconds):
                raw = Path(image_path).read_bytes()
                image = folder / f"sample-{slug}-{instant.strftime('%Y%m%dT%H%M%S%fZ')}.jpg"
                record["image"] = str(image.relative_to(self.root)).replace(os.sep, "/")
            else:
                raw = b""
            evidence = []
            for label, data in (extra_images or {}).items() if images_enabled else ():
                label = re.sub(r"[^a-zA-Z0-9_-]", "_", str(label))[:40]
                path = folder / f"sample-evidence-{slug}-{label}-{instant.strftime('%Y%m%dT%H%M%S%fZ')}.jpg"
                evidence.append((path, data))
                record["evidence"][label] = str(path.relative_to(self.root)).replace(os.sep, "/")
            if not images_enabled:
                record['image_skipped'] = 'history_paused'
            payload = ((json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                       if diagnostics_enabled else b'')
            needed = len(raw) + len(payload) + sum(len(data) for _, data in evidence)
            self._prune(now, needed, protect=journal)
            if (self._bytes + needed > self.max_bytes
                    or shutil.disk_usage(self.root).free - needed < self.min_free_bytes):
                image, raw, evidence = None, b"", []
                record.update(image=None, evidence={}, image_skipped="storage_limit")
                payload = ((json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                           if diagnostics_enabled else b'')
                self._prune(now, len(payload), protect=journal)
                if payload and (self._bytes + len(payload) > self.max_bytes
                        or shutil.disk_usage(self.root).free - len(payload) < self.min_free_bytes):
                    raise OSError("Observation storage budget exhausted")
            if image is not None or evidence or diagnostics_enabled:
                folder.mkdir(parents=True, exist_ok=True)
            if image is not None:
                with image.open("xb") as stream:
                    stream.write(raw)
                self._track(image, now)
                self._last_snapshot[slug] = now
            for path, data in evidence:
                with path.open("xb") as stream:
                    stream.write(data)
                self._track(path, now)
            if diagnostics_enabled:
                with journal.open("ab") as stream:
                    stream.write(payload)
                self._track(journal, now)
            return {"recorded": True, "image_saved": image is not None,
                    "evidence_saved": len(evidence),
                    "diagnostics_saved": diagnostics_enabled,
                    "bytes_used": self._bytes, "max_bytes": self.max_bytes,
                    "retention_days": self.max_age / 86400,
                    "journal": (str(journal.relative_to(self.root)).replace(os.sep, "/")
                                if diagnostics_enabled else None)}
