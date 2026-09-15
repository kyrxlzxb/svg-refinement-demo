from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Any, Iterable


POINT_TOLERANCE = 1e-7
MAX_MANIFEST_SEGMENTS = 80


def _same_point(left: complex, right: complex) -> bool:
    return abs(left - right) <= POINT_TOLERANCE


def _xy(point: complex) -> list[float]:
    return [round(point.real, 4), round(point.imag, 4)]


@dataclass
class SegmentRecord:
    index: int
    kind: str
    start_ref: str
    end_ref: str
    control_refs: tuple[str, ...]
    original: Any


class NormalizedArrowPath:
    """Addressable absolute path geometry with stable per-path point IDs."""

    def __init__(self, path_id: str, d: str) -> None:
        try:
            from svgpathtools import parse_path
        except ImportError as exc:
            raise RuntimeError("svgpathtools is required; install requirements.txt") from exc

        self.path_id = path_id
        self.original_d = d
        self.path = parse_path(d)
        if not self.path:
            raise ValueError(f"Path {path_id} is empty")
        self.was_closed = bool(self.path.isclosed())
        self.points: dict[str, complex] = {}
        self.records: list[SegmentRecord] = []
        self.subpath_count = 0
        self._build_records()

    def _new_node(self, value: complex) -> str:
        ref = f"node_{sum(key.startswith('node_') for key in self.points):03d}"
        self.points[ref] = value
        return ref

    def _build_records(self) -> None:
        previous_end: complex | None = None
        previous_end_ref: str | None = None
        subpath_start: complex | None = None
        subpath_start_ref: str | None = None

        for index, segment in enumerate(self.path):
            if previous_end is None or not _same_point(segment.start, previous_end):
                self.subpath_count += 1
                start_ref = self._new_node(segment.start)
                subpath_start, subpath_start_ref = segment.start, start_ref
            else:
                start_ref = previous_end_ref
            assert start_ref is not None

            if subpath_start is not None and _same_point(segment.end, subpath_start):
                end_ref = subpath_start_ref
            else:
                end_ref = self._new_node(segment.end)
            assert end_ref is not None

            kind = type(segment).__name__
            controls: list[str] = []
            if kind == "CubicBezier":
                for label, value in (("control_1", segment.control1), ("control_2", segment.control2)):
                    ref = f"segment_{index:03d}.{label}"
                    self.points[ref] = value
                    controls.append(ref)
            elif kind == "QuadraticBezier":
                ref = f"segment_{index:03d}.control"
                self.points[ref] = segment.control
                controls.append(ref)

            self.records.append(SegmentRecord(index, kind, start_ref, end_ref, tuple(controls), segment))
            previous_end, previous_end_ref = segment.end, end_ref

    def qualify(self, local_ref: str) -> str:
        return f"{self.path_id}.{local_ref}"

    def local_ref(self, reference: str) -> str:
        prefix = f"{self.path_id}."
        local = reference[len(prefix) :] if reference.startswith(prefix) else reference
        if local not in self.points:
            raise ValueError(f"Unknown point reference for {self.path_id}: {reference}")
        return local

    def get(self, reference: str) -> complex:
        return self.points[self.local_ref(reference)]

    def set(self, reference: str, value: complex) -> None:
        if not math.isfinite(value.real) or not math.isfinite(value.imag):
            raise ValueError("Arrow point coordinates must be finite")
        self.points[self.local_ref(reference)] = value

    def _segment_lengths(self) -> list[float]:
        lengths = []
        for segment in self.path:
            try:
                lengths.append(float(segment.length(error=1e-4)))
            except Exception:
                lengths.append(abs(segment.end - segment.start))
        return lengths

    def manifest(self) -> dict[str, Any]:
        if len(self.records) > MAX_MANIFEST_SEGMENTS:
            return {
                "segment_count": len(self.records),
                "omitted": True,
                "reason": f"Path exceeds the {MAX_MANIFEST_SEGMENTS}-segment demo limit",
            }
        lengths = self._segment_lengths()
        positive_neighbors = [value for value in lengths[:-1] if value > POINT_TOLERANCE]
        closing_ratio = lengths[-1] / median(positive_neighbors) if positive_neighbors else None
        segments = []
        for record, length in zip(self.records, lengths):
            item = {
                "id": f"{self.path_id}.segment_{record.index:03d}",
                "type": record.kind,
                "start": {"id": self.qualify(record.start_ref), "point": _xy(self.points[record.start_ref])},
                "end": {"id": self.qualify(record.end_ref), "point": _xy(self.points[record.end_ref])},
                "length": round(length, 4),
            }
            if record.control_refs:
                item["controls"] = [
                    {"id": self.qualify(ref), "point": _xy(self.points[ref])} for ref in record.control_refs
                ]
            segments.append(item)
        return {
            "absolute_coordinates": True,
            "closed": self.was_closed,
            "subpath_count": self.subpath_count,
            "segment_count": len(self.records),
            "segments": segments,
            "diagnostics": {
                "closing_segment_length_ratio": round(closing_ratio, 3) if closing_ratio is not None else None,
                "suspicious_closing_segment": bool(closing_ratio is not None and closing_ratio >= 4.0),
            },
        }

    def bbox(self) -> tuple[float, float, float, float]:
        path = self.rebuild()
        xmin, xmax, ymin, ymax = path.bbox()
        return xmin, ymin, xmax, ymax

    def _expanded_translation_refs(self, references: Iterable[str]) -> set[str]:
        local_refs = {self.local_ref(reference) for reference in references}
        for record in self.records:
            if record.start_ref in local_refs and record.end_ref in local_refs:
                local_refs.update(record.control_refs)
        return local_refs

    def translate_points(self, references: Iterable[str], delta: complex) -> list[str]:
        local_refs = self._expanded_translation_refs(references)
        if not local_refs:
            raise ValueError("No arrow points were selected for translation")
        for reference in local_refs:
            self.points[reference] += delta
        return [self.qualify(reference) for reference in sorted(local_refs)]

    def mirror_pairs(self, pairs: list[dict[str, Any]], *, axis: str, centerline: float) -> list[dict[str, str]]:
        if not pairs:
            raise ValueError("Mirror repair requires at least one source/target pair")
        source_values = [self.get(str(pair.get("source_id", ""))) for pair in pairs]
        targets = [self.local_ref(str(pair.get("target_id", ""))) for pair in pairs]
        if len(set(targets)) != len(targets):
            raise ValueError("Mirror repair contains duplicate target points")
        applied = []
        for pair, source in zip(pairs, source_values):
            source_ref = str(pair.get("source_id", ""))
            target_ref = str(pair.get("target_id", ""))
            if axis == "x":
                mirrored = complex(2 * centerline - source.real, source.imag)
            else:
                mirrored = complex(source.real, 2 * centerline - source.imag)
            self.set(target_ref, mirrored)
            applied.append({"source_id": source_ref, "target_id": target_ref})
        return applied

    def rebuild(self):
        from svgpathtools import Arc, CubicBezier, Line, Path, QuadraticBezier

        rebuilt = []
        for record in self.records:
            start, end = self.points[record.start_ref], self.points[record.end_ref]
            original = record.original
            if record.kind == "Line":
                segment = Line(start, end)
            elif record.kind == "CubicBezier":
                segment = CubicBezier(start, self.points[record.control_refs[0]], self.points[record.control_refs[1]], end)
            elif record.kind == "QuadraticBezier":
                segment = QuadraticBezier(start, self.points[record.control_refs[0]], end)
            elif record.kind == "Arc":
                segment = Arc(start, original.radius, original.rotation, original.large_arc, original.sweep, end)
            else:
                raise ValueError(f"Unsupported arrow segment type: {record.kind}")
            rebuilt.append(segment)
        return Path(*rebuilt)

    def to_d(self) -> str:
        return self.rebuild().d(use_closed_attrib=self.was_closed)


def normalized_path_manifest(path_id: str, d: str) -> dict[str, Any]:
    try:
        return NormalizedArrowPath(path_id, d).manifest()
    except Exception as exc:
        return {"normalization_error": str(exc)}


def axis_for_direction(direction: str) -> str:
    normalized = direction.upper()
    if normalized == "UP":
        return "x"
    if normalized in {"LEFT", "RIGHT"}:
        return "y"
    raise ValueError("head_direction must be UP, LEFT, or RIGHT")


def centerline_from_boundaries(model: NormalizedArrowPath, references: list[str], axis: str) -> tuple[float, float]:
    if not isinstance(references, list) or len(references) != 2:
        raise ValueError("Exactly two shaft_boundary_ids are required")
    points = [model.get(reference) for reference in references]
    values = [point.real if axis == "x" else point.imag for point in points]
    width = abs(values[1] - values[0])
    if width <= POINT_TOLERANCE:
        raise ValueError("Shaft boundary mapping has zero width")
    return sum(values) / 2, width


def validate_geometry_change(
    model: NormalizedArrowPath,
    *,
    old_bbox: tuple[float, float, float, float],
    old_shaft_width: float,
    boundary_refs: list[str],
    axis: str,
    max_displacement: float,
) -> None:
    rebuilt = model.rebuild()
    if model.was_closed and not rebuilt.isclosed():
        raise ValueError("Arrow repair unexpectedly opened a closed path")
    _center, new_width = centerline_from_boundaries(model, boundary_refs, axis)
    if not math.isclose(old_shaft_width, new_width, rel_tol=0.02, abs_tol=1e-5):
        raise ValueError("Arrow repair unexpectedly changed shaft width")
    new_bbox = model.bbox()
    old_dimensions = (old_bbox[2] - old_bbox[0], old_bbox[3] - old_bbox[1])
    new_dimensions = (new_bbox[2] - new_bbox[0], new_bbox[3] - new_bbox[1])
    for old, new in zip(old_dimensions, new_dimensions):
        if old > POINT_TOLERANCE and not 0.5 <= new / old <= 2.0:
            raise ValueError("Arrow repair changed the overall bounding box too much")
    if max_displacement > max(max(old_dimensions), 1.0) * 0.75:
        raise ValueError("Arrow repair requires an unsafe point displacement")
