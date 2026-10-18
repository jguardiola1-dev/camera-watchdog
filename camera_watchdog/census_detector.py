"""Experimental scene-change diagnostics. Never publishes alarm states.

Dense Census descriptors compare local intensity ordering. These are not ORB
matches or physical orientation estimates. All thresholds need real calibration.
"""

from dataclasses import dataclass
import math

import cv2
import numpy as np
from reference_catalog import REFERENCE_KINDS


@dataclass(frozen=True)
class CensusSettings:
    width: int = 320
    grid_columns: int = 8
    grid_rows: int = 6
    texture_range: float = 4.0
    min_texture_ratio: float = 0.03
    min_block_pixels: int = 16
    min_valid_blocks: int = 12
    block_distance: float = 0.25
    changed_block_ratio: float = 0.60
    stable_block_ratio: float = 0.15
    jitter_pixels: int = 1
    confirmations: int = 2
    max_gap_seconds: float = 180.0

    @classmethod
    def from_options(cls, options):
        values = {}
        for name in ("block_distance", "changed_block_ratio", "stable_block_ratio"):
            value = float(options.get("census_" + name, getattr(cls(), name)))
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError("Invalid Census threshold")
            values[name] = value
        if values["stable_block_ratio"] >= values["changed_block_ratio"]:
            raise ValueError("Stable threshold must precede change threshold")
        confirmations = int(options.get("census_confirmations", 2))
        if not 2 <= confirmations <= 10:
            raise ValueError("Invalid Census confirmation count")
        return cls(**values, confirmations=confirmations)


_POPCOUNT = np.array([bin(n).count("1") for n in range(256)], dtype=np.uint8)


def _prepare(image, size):
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(image.astype(np.float32), size, interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    height, width = gray.shape
    center = gray[2:height - 2, 2:width - 2]
    descriptor = np.zeros(center.shape, dtype=np.uint32)
    bit = 0
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            if dx == 0 and dy == 0:
                continue
            neighbor = gray[2 + dy:height - 2 + dy, 2 + dx:width - 2 + dx]
            descriptor |= (neighbor < center).astype(np.uint32) << bit
            bit += 1
    kernel = np.ones((5, 5), dtype=np.uint8)
    local_range = (cv2.dilate(gray, kernel) - cv2.erode(gray, kernel))[2:-2, 2:-2]
    return descriptor, local_range


def _distance(a, b):
    value = a ^ b
    count = np.zeros(value.shape, dtype=np.uint8)
    for bits in (0, 8, 16, 24):
        count += _POPCOUNT[((value >> bits) & 255).astype(np.uint8)]
    return count.astype(np.float32) / 24


def compare_scene(reference, current, settings=None):
    s = settings or CensusSettings()
    result = {"algorithm": "census_blocks_experimental", "mode": "offline_only",
              "status": "inconclusive", "reason": "invalid_image",
              "reliable": False, "scene_change": False, "valid_blocks": 0,
              "changed_blocks": 0, "changed_block_ratio": None,
              "mean_distance": None, "blocks": []}
    if reference is None or current is None or not reference.size or not current.size:
        return result


    rh, rw = reference.shape[:2]
    ch, cw = current.shape[:2]
    if abs((rw / rh) / (cw / ch) - 1) > 0.01:
        result["reason"] = "aspect_ratio_changed"
        return result
    width = min(s.width, rw, cw)
    height = round(width * rh / rw)
    if min(width, height) < 32:
        result["reason"] = "image_too_small"
        return result
    a, ta = _prepare(reference, (width, height))
    b, tb = _prepare(current, (width, height))
    texture_a, texture_b = float(np.mean(ta >= s.texture_range)), float(np.mean(tb >= s.texture_range))
    result.update(processed_width=width, processed_height=height,
                  texture_reference_ratio=round(texture_a, 4),
                  texture_current_ratio=round(texture_b, 4),
                  brightness_reference=round(float(np.mean(reference)), 3),
                  brightness_current=round(float(np.mean(current)), 3))
    if min(texture_a, texture_b) < s.min_texture_ratio:
        result["reason"] = "insufficient_texture"
        return result
    margin = s.jitter_pixels
    descriptor_height, descriptor_width = a.shape
    rows = slice(margin, descriptor_height - margin)
    cols = slice(margin, descriptor_width - margin)
    base = a[rows, cols]
    valid = ta[rows, cols] >= s.texture_range
    best = np.ones(base.shape, dtype=np.float32)
    for dy in range(-margin, margin + 1):
        for dx in range(-margin, margin + 1):
            shifted = b[margin + dy:descriptor_height - margin + dy,
                        margin + dx:descriptor_width - margin + dx]
            textured = tb[margin + dy:descriptor_height - margin + dy,
                          margin + dx:descriptor_width - margin + dx] >= s.texture_range
            cost = _distance(base, shifted)
            best = np.minimum(best, np.where(textured, cost, 1.0))
    height, width = base.shape
    distances, changed_cells = [], []
    for row in range(s.grid_rows):
        y0, y1 = row * height // s.grid_rows, (row + 1) * height // s.grid_rows
        for col in range(s.grid_columns):
            x0, x1 = col * width // s.grid_columns, (col + 1) * width // s.grid_columns
            mask = valid[y0:y1, x0:x1]
            count = int(mask.sum())
            block = {"row": row, "column": col, "valid_pixels": count,
                     "distance": None, "changed": None}
            if count >= s.min_block_pixels:
                score = float(np.mean(best[y0:y1, x0:x1][mask]))
                changed = score >= s.block_distance
                block.update(distance=round(score, 5), changed=changed)
                distances.append(score)
                if changed:
                    changed_cells.append((row, col))
            result["blocks"].append(block)
    result.update(valid_blocks=len(distances), changed_blocks=len(changed_cells))
    if len(distances) < s.min_valid_blocks:
        result["reason"] = "insufficient_spatial_texture"
        return result
    ratio = len(changed_cells) / len(distances)
    spread = (len({row for row, _ in changed_cells}) >= 3
              and len({col for _, col in changed_cells}) >= 3)
    changed = ratio >= s.changed_block_ratio and spread
    stable = ratio <= s.stable_block_ratio
    result.update(reliable=True, scene_change=changed,
                  status="scene_change" if changed else "stable" if stable else "inconclusive",
                  reason="widespread_structure_change" if changed else "structure_preserved" if stable else "partial_structure_change",
                  changed_block_ratio=round(ratio, 5),
                  mean_distance=round(float(np.mean(distances)), 5),
                  thresholds={"block_distance": s.block_distance,
                              "changed_block_ratio": s.changed_block_ratio,
                              "stable_block_ratio": s.stable_block_ratio})
    return result


class SceneObserver:
    """Freeze last stable image on suspicion. Never save/promote server references."""

    def __init__(self, settings=None):
        self.settings = settings or CensusSettings()
        self.anchor = None
        self.anchor_id = None
        self.pending = 0
        self.first_changed_id = None
        self.last_time = None
        self.latched = False

    def observe(self, image, capture_id, timestamp):
        timestamp = float(timestamp)
        if not math.isfinite(timestamp):
            raise ValueError("Invalid capture timestamp")
        if self.last_time is not None and timestamp <= self.last_time:
            raise ValueError("Captures must be distinct and chronological")
        gap = self.last_time is not None and timestamp - self.last_time > self.settings.max_gap_seconds
        self.last_time = timestamp
        if gap:
            self.pending = 0
        if self.anchor is None:
            quality = compare_scene(image, image, self.settings)
            if not quality["reliable"]:
                quality.update(pending=0, required=self.settings.confirmations,
                               observed_scene_loss=False, anchor_id=None,
                               alarms_published=False)
                return quality
            self.anchor = image.copy()
            self.anchor_id = capture_id
            quality.update(status="initialized", reason="initial_scene_not_validated",
                           pending=0, required=self.settings.confirmations,
                           observed_scene_loss=False, anchor_id=capture_id,
                           alarms_published=False)
            return quality
        result = compare_scene(self.anchor, image, self.settings)
        previous_anchor_id = self.anchor_id
        if result["reliable"] and result["scene_change"]:
            if self.first_changed_id is None:
                self.first_changed_id = capture_id
            self.pending = min(self.pending + 1, self.settings.confirmations)
            if self.pending >= self.settings.confirmations:
                self.latched = True
        elif result["reliable"] and result["status"] == "stable":
            self.pending = 0
            self.first_changed_id = None
            self.latched = False
            self.anchor = image.copy()
            self.anchor_id = capture_id
        else:
            # Unknown quality does not authorize adopting a different orientation.
            self.pending = 0
        result.update(anchor_id=previous_anchor_id,
                      first_changed_id=self.first_changed_id, pending=self.pending,
                      required=self.settings.confirmations,
                      observed_scene_loss=self.latched,
                      alarms_published=False)
        return result


class ReferenceSceneObserver(SceneObserver):
    """Compare only saved day/night references, without learning scenes."""

    def __init__(self, settings=None):
        super().__init__(settings)
        self.reference_ids = None

    def observe(self, image, capture_id, timestamp, references):
        timestamp = float(timestamp)
        if not math.isfinite(timestamp):
            raise ValueError("Invalid capture timestamp")
        if self.last_time is not None and timestamp <= self.last_time:
            raise ValueError("Captures must be distinct and chronological")
        if self.last_time is not None and timestamp - self.last_time > self.settings.max_gap_seconds:
            self.pending = 0
        self.last_time = timestamp
        names = tuple(name for name in REFERENCE_KINDS if name in references)
        ids = tuple((name, references.get(name, (None, None))[0]) for name in names)
        if ids != self.reference_ids:
            self.pending = 0
            self.first_changed_id = None
            self.reference_ids = ids
        comparisons = {}
        for name in names:
            ref_id, reference = references.get(name, (None, None))
            result = compare_scene(reference, image, self.settings)
            result['reference_id'] = ref_id
            comparisons[name] = result
        if not comparisons:
            result = compare_scene(None, image, self.settings)
            result.update(comparison_mode='day_night_references', reason='no_saved_reference',
                          reference_comparisons={}, pending=0, required=self.settings.confirmations,
                          observed_scene_loss=self.latched, alarms_published=False)
            return result
        stable = [name for name, r in comparisons.items() if r['status'] == 'stable']
        changed = all(r['reliable'] and r['scene_change'] for r in comparisons.values())
        valid = [name for name, r in comparisons.items() if r['reliable']]
        candidates = stable or valid or list(comparisons)
        selected = min(candidates, key=lambda name: comparisons[name]['changed_block_ratio']
                       if comparisons[name]['changed_block_ratio'] is not None else float('inf'))
        result = dict(comparisons[selected])
        self.anchor_id, self.anchor = references.get(selected, (None, None))
        if stable:
            self.pending = 0
            self.first_changed_id = None
            self.latched = False
            result.update(status='stable', reason='saved_reference_match', scene_change=False)
        elif changed:
            if self.first_changed_id is None:
                self.first_changed_id = capture_id
            self.pending = min(self.pending + 1, self.settings.confirmations)
            self.latched = self.latched or self.pending >= self.settings.confirmations
            result.update(status='scene_change', reason=('both_references_changed' if len(names) == 2
                                                       else 'all_references_changed'), scene_change=True)
        else:
            self.pending = 0
            if not self.latched:
                self.first_changed_id = None
            result.update(status='inconclusive', reason='no_reference_match', scene_change=False,
                          reliable=all(r['reliable'] for r in comparisons.values()))
        result.update(comparison_mode='day_night_references', reference=selected,
                      reference_comparisons={name: {k: v for k, v in r.items() if k != 'blocks'}
                                             for name, r in comparisons.items()},
                      anchor_id=self.anchor_id, first_changed_id=self.first_changed_id,
                      pending=self.pending, required=self.settings.confirmations,
                      observed_scene_loss=self.latched, alarms_published=False)
        return result
