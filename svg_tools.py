from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from defusedxml import ElementTree as SafeET
from xml.etree import ElementTree as ET

from arrow_tools import (
    NormalizedArrowPath,
    axis_for_direction,
    centerline_from_boundaries,
    normalized_path_manifest,
    validate_geometry_change,
)


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
RELEVANT_TAGS = {"g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text"}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def number(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    match = re.match(r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)", value)
    return float(match.group(1)) if match else default


@dataclass(frozen=True)
class Matrix:
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def __matmul__(self, other: "Matrix") -> "Matrix":
        return Matrix(
            self.a * other.a + self.c * other.b,
            self.b * other.a + self.d * other.b,
            self.a * other.c + self.c * other.d,
            self.b * other.c + self.d * other.d,
            self.a * other.e + self.c * other.f + self.e,
            self.b * other.e + self.d * other.f + self.f,
        )

    def point(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f

    def inverse_vector(self, dx: float, dy: float) -> tuple[float, float]:
        determinant = self.a * self.d - self.b * self.c
        if abs(determinant) < 1e-12:
            raise ValueError("Cannot translate inside a singular parent transform")
        return ((self.d * dx - self.c * dy) / determinant, (-self.b * dx + self.a * dy) / determinant)


def parse_transform(value: str | None) -> Matrix:
    result = Matrix()
    if not value:
        return result
    for name, raw_args in re.findall(r"([A-Za-z]+)\s*\(([^)]*)\)", value):
        args = [float(v) for v in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", raw_args)]
        name = name.lower()
        if name == "matrix" and len(args) == 6:
            item = Matrix(*args)
        elif name == "translate" and args:
            item = Matrix(e=args[0], f=args[1] if len(args) > 1 else 0.0)
        elif name == "scale" and args:
            item = Matrix(a=args[0], d=args[1] if len(args) > 1 else args[0])
        elif name == "rotate" and args:
            angle = math.radians(args[0])
            rotation = Matrix(a=math.cos(angle), b=math.sin(angle), c=-math.sin(angle), d=math.cos(angle))
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                item = Matrix(e=cx, f=cy) @ rotation @ Matrix(e=-cx, f=-cy)
            else:
                item = rotation
        else:
            continue
        result = result @ item
    return result


def _transformed_bbox(bounds: tuple[float, float, float, float], transform: Matrix) -> list[float]:
    x0, y0, x1, y1 = bounds
    points = [transform.point(x, y) for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _local_bbox(element: ET.Element) -> tuple[float, float, float, float] | None:
    tag = local_name(element.tag)
    if tag == "rect":
        x, y = number(element.get("x")), number(element.get("y"))
        return x, y, x + number(element.get("width")), y + number(element.get("height"))
    if tag == "circle":
        cx, cy, r = number(element.get("cx")), number(element.get("cy")), number(element.get("r"))
        return cx - r, cy - r, cx + r, cy + r
    if tag == "ellipse":
        cx, cy = number(element.get("cx")), number(element.get("cy"))
        rx, ry = number(element.get("rx")), number(element.get("ry"))
        return cx - rx, cy - ry, cx + rx, cy + ry
    if tag == "line":
        x1, y1, x2, y2 = (number(element.get(k)) for k in ("x1", "y1", "x2", "y2"))
        return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
    if tag in {"polyline", "polygon"}:
        values = [float(v) for v in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", element.get("points", ""))]
        points = list(zip(values[::2], values[1::2]))
        if points:
            xs, ys = zip(*points)
            return min(xs), min(ys), max(xs), max(ys)
    if tag == "path" and element.get("d"):
        try:
            from svgpathtools import parse_path

            xmin, xmax, ymin, ymax = parse_path(element.get("d", "")).bbox()
            return xmin, ymin, xmax, ymax
        except Exception:
            return None
    if tag == "text":
        x, y = number(element.get("x")), number(element.get("y"))
        font_size = number(element.get("font-size"), 16.0)
        text = "".join(element.itertext())
        width = number(element.get("textLength"), max(1, len(text)) * font_size * 0.58)
        anchor = element.get("text-anchor", "start")
        x0 = x - width / 2 if anchor == "middle" else x - width if anchor == "end" else x
        return x0, y - font_size, x0 + width, y + font_size * 0.25
    return None


class SvgDocument:
    def __init__(self, tree: ET.ElementTree) -> None:
        self.tree = tree
        self.root = tree.getroot()
        self.assign_stable_ids()

    @classmethod
    def load(cls, path: Path) -> "SvgDocument":
        return cls(SafeET.parse(path))

    def clone(self) -> "SvgDocument":
        return SvgDocument(ET.ElementTree(copy.deepcopy(self.root)))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.tree.write(path, encoding="utf-8", xml_declaration=True)

    def assign_stable_ids(self) -> None:
        used = {value for element in self.root.iter() if (value := element.get("id"))}
        counters: dict[str, int] = {}
        for element in self.root.iter():
            tag = local_name(element.tag)
            if tag not in RELEVANT_TAGS or element.get("id"):
                continue
            counters[tag] = counters.get(tag, 0) + 1
            while True:
                candidate = f"{tag}_{counters[tag]:03d}"
                if candidate not in used:
                    break
                counters[tag] += 1
            element.set("id", candidate)
            used.add(candidate)

    def _walk(self) -> Iterable[tuple[ET.Element, ET.Element | None, Matrix, Matrix, int]]:
        z = 0

        def visit(element: ET.Element, parent: ET.Element | None, parent_matrix: Matrix):
            nonlocal z
            own = parse_transform(element.get("transform"))
            world = parent_matrix @ own
            current_z = z
            z += 1
            yield element, parent, parent_matrix, world, current_z
            for child in list(element):
                yield from visit(child, element, world)

        yield from visit(self.root, None, Matrix())

    def index(self) -> dict[str, tuple[ET.Element, ET.Element | None, Matrix, Matrix, int]]:
        return {element.get("id"): (element, parent, parent_matrix, world, z) for element, parent, parent_matrix, world, z in self._walk() if element.get("id")}

    def manifest(self) -> dict[str, Any]:
        view_box = [number(v) for v in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", self.root.get("viewBox", ""))]
        canvas = {
            "viewBox": view_box if len(view_box) == 4 else None,
            "width": self.root.get("width"),
            "height": self.root.get("height"),
        }
        elements = []
        for element, parent, _parent_matrix, world, z in self._walk():
            tag = local_name(element.tag)
            element_id = element.get("id")
            if tag not in RELEVANT_TAGS or not element_id:
                continue
            local_bbox = _local_bbox(element)
            bbox = _transformed_bbox(local_bbox, world) if local_bbox else None
            item: dict[str, Any] = {
                "id": element_id,
                "type": tag,
                "parent_id": parent.get("id") if parent is not None else None,
                "z_order": z,
                "fill": element.get("fill"),
                "stroke": element.get("stroke"),
                "transform": element.get("transform"),
                "bbox": [round(v, 3) for v in bbox] if bbox else None,
                "center": [round((bbox[0] + bbox[2]) / 2, 3), round((bbox[1] + bbox[3]) / 2, 3)] if bbox else None,
            }
            if tag == "text":
                item.update(
                    text="".join(element.itertext()),
                    font_family=element.get("font-family"),
                    font_size=element.get("font-size"),
                    text_anchor=element.get("text-anchor"),
                )
            elif tag == "path":
                item["path_d"] = element.get("d")
                item["normalized_path"] = normalized_path_manifest(element_id, element.get("d", ""))
            elements.append(item)
        return {"canvas": canvas, "bbox_note": "Text and transformed curved-path bboxes are estimates; use the render as visual truth.", "elements": elements}

    def apply_action(self, action: dict[str, Any]) -> dict[str, Any]:
        operation = str(action.get("type", ""))
        handlers = {
            "REPLACE_TEXT": self._replace_text,
            "FIT_TEXT_WIDTH": self._fit_text_width,
            "ALIGN_COMPOSITE_CENTER": self._align_composite_center,
            "REORDER_ELEMENTS": self._reorder_elements,
            "REPAIR_ARROW_INTERNAL": self._repair_arrow_internal,
            "ALIGN_ARROW_GROUP": self._align_arrow_group,
        }
        if operation not in handlers:
            raise ValueError(f"Unsupported operation: {operation}")
        details = handlers[operation](action)
        return {"operation": operation, **details}

    def _one_target(self, action: dict[str, Any]) -> tuple[str, ET.Element]:
        targets = action.get("target_ids")
        if not isinstance(targets, list) or len(targets) != 1:
            raise ValueError("This operation requires exactly one target ID")
        target_id = str(targets[0])
        record = self.index().get(target_id)
        if not record:
            raise ValueError(f"Unknown target ID: {target_id}")
        return target_id, record[0]

    def _replace_text(self, action: dict[str, Any]) -> dict[str, Any]:
        target_id, element = self._one_target(action)
        if local_name(element.tag) != "text":
            raise ValueError("REPLACE_TEXT target must be a text element")
        replacement = action.get("replacement_text")
        if not isinstance(replacement, str):
            raise ValueError("REPLACE_TEXT requires replacement_text")
        if list(element):
            raise ValueError("REPLACE_TEXT currently supports only text elements without child tspans")
        old_text = "".join(element.itertext())
        element.text = replacement
        return {"target_id": target_id, "old_text": old_text, "new_text": replacement}

    def _fit_text_width(self, action: dict[str, Any]) -> dict[str, Any]:
        target_id, element = self._one_target(action)
        if local_name(element.tag) != "text":
            raise ValueError("FIT_TEXT_WIDTH target must be a text element")
        width = float(action.get("target_width", 0))
        if not math.isfinite(width) or width <= 0:
            raise ValueError("FIT_TEXT_WIDTH requires a positive target_width")
        old = element.get("textLength")
        element.set("textLength", f"{width:.3f}".rstrip("0").rstrip("."))
        element.set("lengthAdjust", "spacingAndGlyphs")
        return {"target_id": target_id, "old_text_length": old, "new_text_length": width}

    def _align_composite_center(self, action: dict[str, Any]) -> dict[str, Any]:
        targets = action.get("target_ids")
        anchor_id = action.get("anchor_id")
        if not isinstance(targets, list) or not targets or not isinstance(anchor_id, str):
            raise ValueError("ALIGN_COMPOSITE_CENTER requires target_ids and anchor_id")
        index = self.index()
        if anchor_id not in index:
            raise ValueError(f"Unknown anchor ID: {anchor_id}")
        anchor_element, _ap, _apm, anchor_world, _az = index[anchor_id]
        alignable_tags = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}
        if local_name(anchor_element.tag) not in alignable_tags:
            raise ValueError("Composite anchor must be a shape element")
        anchor_local_bbox = _local_bbox(anchor_element)
        if not anchor_local_bbox:
            raise ValueError("Anchor does not have a calculable bounding box")
        anchor_bbox = _transformed_bbox(anchor_local_bbox, anchor_world)
        anchor_center = ((anchor_bbox[0] + anchor_bbox[2]) / 2, (anchor_bbox[1] + anchor_bbox[3]) / 2)
        moves = []
        for raw_id in targets:
            target_id = str(raw_id)
            if target_id == anchor_id:
                continue
            if target_id not in index:
                raise ValueError(f"Unknown target ID: {target_id}")
            element, _parent, parent_matrix, world, _z = index[target_id]
            if local_name(element.tag) not in alignable_tags:
                raise ValueError(f"Composite target {target_id} must be a shape element")
            bounds = _local_bbox(element)
            if not bounds:
                raise ValueError(f"Target {target_id} has no calculable bounding box")
            bbox = _transformed_bbox(bounds, world)
            center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
            global_dx, global_dy = anchor_center[0] - center[0], anchor_center[1] - center[1]
            local_dx, local_dy = parent_matrix.inverse_vector(global_dx, global_dy)
            previous = element.get("transform", "").strip()
            translation = f"translate({local_dx:.6g} {local_dy:.6g})"
            element.set("transform", f"{translation} {previous}".strip())
            moves.append({"target_id": target_id, "dx": global_dx, "dy": global_dy})
        return {"anchor_id": anchor_id, "moves": moves}

    def _reorder_elements(self, action: dict[str, Any]) -> dict[str, Any]:
        targets = action.get("target_ids")
        anchor_id = action.get("anchor_id")
        placement = str(action.get("placement", "")).upper()
        if not isinstance(targets, list) or not targets or not isinstance(anchor_id, str):
            raise ValueError("REORDER_ELEMENTS requires target_ids and anchor_id")
        if placement not in {"BEFORE", "AFTER"}:
            raise ValueError("REORDER_ELEMENTS placement must be BEFORE or AFTER")
        index = self.index()
        if anchor_id not in index:
            raise ValueError(f"Unknown anchor ID: {anchor_id}")
        anchor, parent, _pm, _w, _z = index[anchor_id]
        if parent is None:
            raise ValueError("Cannot reorder relative to the SVG root")
        sibling_positions = {child: position for position, child in enumerate(list(parent))}
        moving = []
        for raw_id in targets:
            target_id = str(raw_id)
            if target_id == anchor_id or target_id not in index:
                raise ValueError(f"Invalid reorder target: {target_id}")
            element, target_parent, *_ = index[target_id]
            if target_parent is not parent:
                raise ValueError("Reordering is only safe for elements with the same direct parent")
            moving.append(element)
        moving.sort(key=sibling_positions.__getitem__)
        for element in moving:
            parent.remove(element)
        anchor_position = list(parent).index(anchor)
        insertion = anchor_position if placement == "BEFORE" else anchor_position + 1
        for offset, element in enumerate(moving):
            parent.insert(insertion + offset, element)
        return {"target_ids": [element.get("id") for element in moving], "anchor_id": anchor_id, "placement": placement}

    def _repair_arrow_internal(self, action: dict[str, Any]) -> dict[str, Any]:
        target_id, element = self._one_target(action)
        if local_name(element.tag) != "path":
            raise ValueError("REPAIR_ARROW_INTERNAL target must be a path")
        index = self.index()
        _element, _parent, _parent_matrix, world, _z = index[target_id]
        if abs(world.b) > 1e-6 or abs(world.c) > 1e-6:
            raise ValueError("Milestone arrow repair does not support rotated or skewed path transforms")

        model = NormalizedArrowPath(target_id, element.get("d", ""))
        if len(model.records) > 80:
            raise ValueError("Arrow exceeds the 80-segment demo safety limit")
        if model.subpath_count != 1:
            raise ValueError("Arrow repair requires one continuous path")
        old_points = dict(model.points)
        old_bbox = model.bbox()
        direction = str(action.get("head_direction", ""))
        axis = axis_for_direction(direction)
        boundary_refs = action.get("shaft_boundary_ids")
        centerline, old_shaft_width = centerline_from_boundaries(model, boundary_refs, axis)
        strategy = str(action.get("strategy", "")).upper()

        if strategy == "TRANSLATE_HEAD":
            apex_id = str(action.get("apex_id", ""))
            apex = model.get(apex_id)
            apex_value = apex.real if axis == "x" else apex.imag
            offset = centerline - apex_value
            delta = complex(offset, 0) if axis == "x" else complex(0, offset)
            moved_refs = model.translate_points(action.get("head_point_ids", []), delta)
            if model.local_ref(apex_id) not in {model.local_ref(ref) for ref in moved_refs}:
                raise ValueError("head_point_ids must include the apex for TRANSLATE_HEAD")
            applied: dict[str, Any] = {"delta": [delta.real, delta.imag], "moved_refs": moved_refs}
        elif strategy == "MIRROR_SIDE":
            pairs = action.get("mirror_pairs")
            if not isinstance(pairs, list):
                raise ValueError("MIRROR_SIDE requires mirror_pairs")
            for pair in pairs:
                if pair.get("source_id") == pair.get("target_id"):
                    raise ValueError("A mirror source and target must be different points")
            applied = {"mirror_pairs": model.mirror_pairs(pairs, axis=axis, centerline=centerline)}
        else:
            raise ValueError("Arrow strategy must be TRANSLATE_HEAD or MIRROR_SIDE")

        apex_id = str(action.get("apex_id", ""))
        apex = model.get(apex_id)
        apex_value = apex.real if axis == "x" else apex.imag
        if abs(apex_value - centerline) > max(old_shaft_width * 0.2, 1.0):
            raise ValueError("Repaired apex is not centered on the connected shaft")
        boundary_points = [model.get(reference) for reference in boundary_refs]
        boundary_cross_axis = sum(point.imag if axis == "x" else point.real for point in boundary_points) / 2
        if direction.upper() == "UP" and apex.imag >= boundary_cross_axis:
            raise ValueError("UP apex is not above its shaft-head boundary landmarks")
        if direction.upper() == "LEFT" and apex.real >= boundary_cross_axis:
            raise ValueError("LEFT apex is not left of its shaft-head boundary landmarks")
        if direction.upper() == "RIGHT" and apex.real <= boundary_cross_axis:
            raise ValueError("RIGHT apex is not right of its shaft-head boundary landmarks")
        max_displacement = max(abs(model.points[key] - old_points[key]) for key in model.points)
        validate_geometry_change(
            model,
            old_bbox=old_bbox,
            old_shaft_width=old_shaft_width,
            boundary_refs=boundary_refs,
            axis=axis,
            max_displacement=max_displacement,
        )
        element.set("d", model.to_d())
        return {
            "target_id": target_id,
            "arrow_type": action.get("arrow_type"),
            "head_direction": direction.upper(),
            "strategy": strategy,
            "centerline": centerline,
            "shaft_width": old_shaft_width,
            "max_point_displacement": max_displacement,
            **applied,
        }

    def _align_arrow_group(self, action: dict[str, Any]) -> dict[str, Any]:
        targets = action.get("target_ids")
        anchor_id = action.get("anchor_id")
        landmarks = action.get("shared_base_landmarks")
        if not isinstance(targets, list) or not targets or not isinstance(anchor_id, str):
            raise ValueError("ALIGN_ARROW_GROUP requires target_ids and anchor_id")
        if not isinstance(landmarks, dict):
            raise ValueError("ALIGN_ARROW_GROUP requires shared_base_landmarks")
        index = self.index()

        def global_base_center(path_id: str) -> tuple[float, float]:
            if path_id not in index:
                raise ValueError(f"Unknown arrow path ID: {path_id}")
            element, _parent, _parent_matrix, world, _z = index[path_id]
            if local_name(element.tag) != "path":
                raise ValueError(f"Arrow group member {path_id} must be a path")
            references = landmarks.get(path_id)
            if not isinstance(references, list) or not references:
                raise ValueError(f"Missing shared-base landmarks for {path_id}")
            model = NormalizedArrowPath(path_id, element.get("d", ""))
            points = [world.point(model.get(ref).real, model.get(ref).imag) for ref in references]
            return sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points)

        anchor_center = global_base_center(anchor_id)
        moves = []
        for raw_id in targets:
            target_id = str(raw_id)
            if target_id == anchor_id:
                continue
            target_center = global_base_center(target_id)
            element, _parent, parent_matrix, world, _z = index[target_id]
            dx, dy = anchor_center[0] - target_center[0], anchor_center[1] - target_center[1]
            bounds = _local_bbox(element)
            if not bounds:
                raise ValueError(f"Arrow {target_id} has no calculable bounding box")
            bbox = _transformed_bbox(bounds, world)
            size = max(bbox[2] - bbox[0], bbox[3] - bbox[1], 1.0)
            if math.hypot(dx, dy) > size * 0.5:
                raise ValueError(f"Arrow group alignment for {target_id} requires an unsafe translation")
            local_dx, local_dy = parent_matrix.inverse_vector(dx, dy)
            previous = element.get("transform", "").strip()
            translation = f"translate({local_dx:.6g} {local_dy:.6g})"
            element.set("transform", f"{translation} {previous}".strip())
            moves.append({"target_id": target_id, "dx": dx, "dy": dy})
        return {"anchor_id": anchor_id, "moves": moves, "shared_region": action.get("shared_region")}


def render_svg(svg_path: Path, png_path: Path) -> None:
    try:
        import cairosvg
    except ImportError as exc:
        raise RuntimeError("CairoSVG is required; install requirements.txt") from exc
    png_path.parent.mkdir(parents=True, exist_ok=True)
    cairosvg.svg2png(url=str(svg_path), write_to=str(png_path))


def render_element_atlas(
    document: SvgDocument,
    svg_path: Path,
    png_path: Path,
    *,
    max_elements: int = 140,
) -> dict[str, Any]:
    """Render isolated, labeled element tiles so visual objects can map to IDs."""
    manifest = document.manifest()
    candidates = [
        item
        for item in manifest["elements"]
        if item["type"] != "g" and item.get("bbox") and item["bbox"][2] > item["bbox"][0] and item["bbox"][3] > item["bbox"][1]
    ]
    shown, omitted = candidates[:max_elements], candidates[max_elements:]
    if not shown:
        raise ValueError("Cannot build an element atlas because no renderable element has a bounding box")

    tile_width, tile_height, columns = 260, 210, 4
    rows = math.ceil(len(shown) / columns)
    atlas = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": str(tile_width * columns),
            "height": str(tile_height * rows),
            "viewBox": f"0 0 {tile_width * columns} {tile_height * rows}",
        },
    )
    original_index = document.index()

    for position, item in enumerate(shown):
        column, row = position % columns, position // columns
        tile_x, tile_y = column * tile_width, row * tile_height
        ET.SubElement(
            atlas,
            f"{{{SVG_NS}}}rect",
            {"x": str(tile_x + 2), "y": str(tile_y + 2), "width": str(tile_width - 4), "height": str(tile_height - 4), "rx": "5", "fill": "#20242a", "stroke": "#6b7280"},
        )
        label = ET.SubElement(
            atlas,
            f"{{{SVG_NS}}}text",
            {"x": str(tile_x + 10), "y": str(tile_y + 22), "fill": "#ffffff", "font-family": "sans-serif", "font-size": "15"},
        )
        label.text = f"{item['id']}  [{item['type']}]"

        x0, y0, x1, y1 = item["bbox"]
        padding = max((x1 - x0) * 0.12, (y1 - y0) * 0.12, 2.0)
        nested_attributes = {
            key: value
            for key, value in document.root.attrib.items()
            if key not in {"x", "y", "width", "height", "viewBox"}
        }
        nested_attributes.update(
            {
                "x": str(tile_x + 8),
                "y": str(tile_y + 30),
                "width": str(tile_width - 16),
                "height": str(tile_height - 40),
                "viewBox": f"{x0 - padding} {y0 - padding} {(x1 - x0) + 2 * padding} {(y1 - y0) + 2 * padding}",
                "preserveAspectRatio": "xMidYMid meet",
            }
        )
        nested = ET.SubElement(atlas, f"{{{SVG_NS}}}svg", nested_attributes)

        keep_ids = {item["id"]}
        record = original_index.get(item["id"])
        parent = record[1] if record else None
        while parent is not None:
            if parent.get("id"):
                keep_ids.add(parent.get("id"))
            parent_record = original_index.get(parent.get("id", ""))
            parent = parent_record[1] if parent_record else None

        cloned_root = copy.deepcopy(document.root)
        for element in cloned_root.iter():
            tag = local_name(element.tag)
            element_id = element.get("id")
            if tag in RELEVANT_TAGS and element_id not in keep_ids:
                existing_style = element.get("style", "").rstrip(";")
                element.set("style", f"{existing_style};display:none!important".lstrip(";"))
        for child in list(cloned_root):
            nested.append(child)
        ET.SubElement(
            nested,
            f"{{{SVG_NS}}}rect",
            {
                "x": str(x0),
                "y": str(y0),
                "width": str(x1 - x0),
                "height": str(y1 - y0),
                "fill": "none",
                "stroke": "#ff3bd4",
                "stroke-width": str(max((x1 - x0), (y1 - y0), 1.0) / 120),
                "stroke-dasharray": "3 2",
                "vector-effect": "non-scaling-stroke",
            },
        )

    svg_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(atlas).write(svg_path, encoding="utf-8", xml_declaration=True)
    render_svg(svg_path, png_path)
    return {
        "shown_ids": [item["id"] for item in shown],
        "omitted_ids": [item["id"] for item in omitted],
        "tile_count": len(shown),
        "note": "Each tile isolates one element at its original transformed position, crops to its bbox, and labels its stable ID.",
    }
