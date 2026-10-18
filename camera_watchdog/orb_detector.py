"""Geometric diagnostics only: this module never publishes Home Assistant states.

Saved references are read-only anchors. No current/previous frame is ever adopted
automatically. Thresholds are starting values for observation, not calibrated alarms.
"""

from dataclasses import dataclass, asdict
import hashlib
import math
from pathlib import Path
import threading
from reference_catalog import REFERENCE_KINDS

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None


@dataclass(frozen=True)
class Settings:
    width: int = 640
    features: int = 1500
    min_matches: int = 30
    min_inliers: int = 20
    min_inlier_ratio: float = 0.55
    max_reprojection_error: float = 2.5
    min_coverage: float = 0.15
    min_grid_cells: int = 6
    translation_percent: float = 1.0
    rotation_degrees: float = 0.75
    scale_percent: float = 1.5
    confirmations: int = 3

    @classmethod
    def from_options(cls, options):
        values = {}
        for name in ("translation_percent", "rotation_degrees", "scale_percent"):
            value = float(options.get("orb_" + name, getattr(cls(), name)))
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid ORB threshold")
            values[name] = value
        confirmations = int(options.get("orb_confirmations", 3))
        if not 2 <= confirmations <= 10:
            raise ValueError("Invalid ORB confirmation count")
        values["confirmations"] = confirmations
        return cls(**values)


def empty_result(reason, status="inconclusive"):
    return {
        "mode": "observation", "status": status, "reason": reason,
        "reliable": False, "keypoints_reference": 0, "keypoints_current": 0,
        "matches": 0, "inliers": 0, "inlier_ratio": 0.0,
        "coverage": 0.0, "grid_cells": 0, "reprojection_error": None,
        "dx": None, "dy": None, "dx_percent": None, "dy_percent": None,
        "rotation": None, "scale": None, "reference": None,
        "pending": 0, "required": 0, "observed_movement": False,
        "processed_width": None, "processed_height": None,
    }


def _gray(image, size):
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    # Local contrast normalization helps modest exposure changes. IR transitions
    # and extreme darkness can still yield an inconclusive result.
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(image)


def _spread(points, width, height):
    hull = cv2.convexHull(points.astype(np.float32))
    coverage = float(cv2.contourArea(hull)) / (width * height)
    cells = {(min(3, max(0, int(x * 4 / width))),
              min(2, max(0, int(y * 3 / height)))) for x, y in points}
    return coverage, len(cells)


def estimate(reference, current, settings=None):
    """Estimate reference -> current geometry; unreliable motion remains None."""
    s = settings or Settings()
    result = empty_result("insufficient_features")
    if cv2 is None:
        return empty_result("opencv_unavailable", "unavailable")
    if reference is None or current is None or not reference.size or not current.size:
        return empty_result("invalid_image")
    rh, rw = reference.shape[:2]
    ch, cw = current.shape[:2]
    result["brightness_current"] = round(float(np.mean(current)), 3)
    result["contrast_current"] = round(float(np.std(current)), 3)
    result["possible_obstruction"] = result["contrast_current"] < 10
    if abs((rw / rh) / (cw / ch) - 1) > 0.01:
        return empty_result("aspect_ratio_changed")
    width = min(s.width, rw, cw)
    height = max(1, round(width * rh / rw))
    result.update(processed_width=width, processed_height=height)
    a, b = _gray(reference, (width, height)), _gray(current, (width, height))
    orb = cv2.ORB_create(nfeatures=s.features, fastThreshold=12, WTA_K=2)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    result.update(keypoints_reference=len(ka), keypoints_current=len(kb))
    if da is None or db is None or min(len(ka), len(kb)) < s.min_matches:
        if result["possible_obstruction"]:
            result["reason"] = "low_texture"
        return result
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    forward = matcher.knnMatch(da, db, k=2)
    reverse = matcher.knnMatch(db, da, k=2)
    backward = {pair[0].queryIdx: pair[0].trainIdx for pair in reverse
                if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance}
    matches = [pair[0] for pair in forward
               if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance
               and backward.get(pair[0].trainIdx) == pair[0].queryIdx]
    result["matches"] = len(matches)
    if len(matches) < s.min_matches:
        result["reason"] = "insufficient_matches"
        return result
    source = np.float32([ka[m.queryIdx].pt for m in matches])
    target = np.float32([kb[m.trainIdx].pt for m in matches])
    matrix, mask = cv2.estimateAffinePartial2D(
        source, target, method=cv2.RANSAC, ransacReprojThreshold=3.0,
        maxIters=2000, confidence=0.99, refineIters=10,
    )
    if matrix is None or mask is None or not np.isfinite(matrix).all():
        result["reason"] = "no_geometric_model"
        return result
    selected = mask.ravel().astype(bool)
    inliers = int(selected.sum())
    ratio = inliers / len(matches)
    result.update(inliers=inliers, inlier_ratio=round(ratio, 4))
    if inliers < s.min_inliers or ratio < s.min_inlier_ratio:
        result["reason"] = "insufficient_inliers"
        return result
    predicted = source[selected] @ matrix[:, :2].T + matrix[:, 2]
    errors = np.linalg.norm(predicted - target[selected], axis=1)
    error = float(np.sqrt(np.mean(errors ** 2)))
    coverage_a, cells_a = _spread(source[selected], width, height)
    coverage_b, cells_b = _spread(target[selected], width, height)
    coverage, cells = min(coverage_a, coverage_b), min(cells_a, cells_b)
    result.update(reprojection_error=round(error, 4),
                  coverage=round(coverage, 4), grid_cells=cells)
    if error > s.max_reprojection_error:
        result["reason"] = "reprojection_error"
        return result
    if coverage < s.min_coverage or cells < s.min_grid_cells:
        result["reason"] = "localized_features"
        return result
    scale = math.hypot(matrix[0, 0], matrix[1, 0])
    if not 0.7 <= scale <= 1.4:
        result["reason"] = "implausible_scale"
        return result
    rotation = math.degrees(math.atan2(matrix[1, 0], matrix[0, 0]))
    center = np.array([width / 2, height / 2])
    delta = matrix[:, :2] @ center + matrix[:, 2] - center
    dx, dy = float(delta[0]), float(delta[1])
    dxp, dyp = 100 * dx / width, 100 * dy / height
    moved = (math.hypot(dxp, dyp) >= s.translation_percent
             or abs(rotation) >= s.rotation_degrees
             or abs(scale - 1) * 100 >= s.scale_percent)
    result.update(
        reliable=True, status="movement" if moved else "stable",
        reason="geometric_movement" if moved else "geometry_stable",
        dx=round(dx, 4), dy=round(dy, 4), dx_percent=round(dxp, 4),
        dy_percent=round(dyp, 4), rotation=round(rotation, 4),
        scale=round(scale, 6),
    )
    return result


class OrbObserver:
    """Track coherent consecutive observations, independently for each camera."""

    def __init__(self):
        self._states = {}
        self._lock = threading.RLock()

    def reset(self, slug):
        with self._lock:
            self._states.pop(slug, None)

    @staticmethod
    def _consistent(a, b, settings):
        # Compare to the FIRST suspect transform, not the preceding transform:
        # a slowly wandering foreground object must not accumulate confirmations.
        angle = abs((a["rotation"] - b["rotation"] + 180) % 360 - 180)
        return (math.hypot(a["dx_percent"] - b["dx_percent"],
                           a["dy_percent"] - b["dy_percent"])
                <= settings.translation_percent * 0.5
                and angle <= settings.rotation_degrees * 0.5
                and abs(a["scale"] - b["scale"]) * 100
                <= settings.scale_percent * 0.5)

    def confirm(self, slug, result, anchor_identity, settings):
        """No inconclusive or stable observation counts towards movement."""
        result = dict(result)
        with self._lock:
            old = self._states.get(slug)
            signature = (anchor_identity, tuple(asdict(settings).items()))
            if result["reliable"] and result["status"] == "movement":
                if (old and old["signature"] == signature
                        and self._consistent(old["first"], result, settings)):
                    pending = min(old["pending"] + 1, settings.confirmations)
                    first = old["first"]
                else:
                    pending, first = 1, dict(result)
                self._states[slug] = {"signature": signature,
                                      "pending": pending, "first": first}
            else:
                pending = 0
                self._states.pop(slug, None)
            result.update(pending=pending, required=settings.confirmations,
                          observed_movement=pending >= settings.confirmations,
                          thresholds={"translation_percent": settings.translation_percent,
                                      "rotation_degrees": settings.rotation_degrees,
                                      "scale_percent": settings.scale_percent})
        return result

    def observe(self, slug, current_path, references, settings=None):
        s = settings or Settings()
        if cv2 is None:
            return self.confirm(slug, empty_result("opencv_unavailable", "unavailable"), "", s)
        # Read into immutable snapshots; never create or overwrite a reference.
        raw = Path(current_path).read_bytes()
        current = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        candidates, identities = [], []
        for kind in ('fixed',) + REFERENCE_KINDS:
            path = references.get(kind)
            if path is None or not Path(path).exists():
                continue
            data = Path(path).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            identities.append((kind, digest))
            reference = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            result = estimate(reference, current, s)
            result["reference"] = kind
            candidates.append(result)
        valid = [r for r in candidates if r["reliable"]]
        if valid:
            best = max(valid, key=lambda r: (r["inliers"] * r["coverage"], -r["reprojection_error"]))
            # Independently saved references may belong to different orientations.
            # A disagreement must never silently authorize a normal position.
            if any(r["status"] != best["status"] or not self._consistent(best, r, s)
                   for r in valid):
                best = empty_result("references_disagree")
        elif candidates:
            best = max(candidates, key=lambda r: r["matches"])
        else:
            best = empty_result("no_saved_reference")
        best["references_checked"] = [
            {k: r[k] for k in ("reference", "status", "reason", "matches", "inliers")}
            for r in candidates
        ]
        return self.confirm(slug, best, tuple(identities), s)
