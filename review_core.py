"""State and SVG edits for the human layout review stage."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from svg_tools import Matrix, SvgDocument, local_name, render_svg


EDITABLE = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text"}
INHERITED = {"fill", "stroke", "stroke-width", "opacity", "font-family", "font-size", "font-weight", "font-style", "text-anchor", "letter-spacing"}
UNSAFE_BROWSER_TAGS = {"script", "foreignObject", "iframe", "audio", "video"}


def _finite(value: Any, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("A finite numeric value is required") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("A finite positive value is required" if positive else "A finite numeric value is required")
    return result


def _style_get(element: ET.Element, name: str) -> str | None:
    style = element.get("style", "")
    for declaration in reversed(style.split(";")):
        key, separator, value = declaration.partition(":")
        if separator and key.strip() == name:
            return value.strip()
    return element.get(name)


def _style_set(element: ET.Element, name: str, value: str) -> None:
    declarations = []
    for declaration in element.get("style", "").split(";"):
        key, separator, _old = declaration.partition(":")
        if separator and key.strip() != name:
            declarations.append(declaration.strip())
    declarations.append(f"{name}:{value}")
    element.set("style", ";".join(declarations))
    element.set(name, value)


def _matrix_text(matrix: Matrix) -> str:
    values = (matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f)
    return "matrix(" + " ".join(f"{value:.10g}" for value in values) + ")"


def _inverse(matrix: Matrix) -> Matrix:
    determinant = matrix.a * matrix.d - matrix.b * matrix.c
    if abs(determinant) < 1e-12:
        raise ValueError("Cannot preserve position through a singular SVG transform")
    a, b, c, d = matrix.d / determinant, -matrix.b / determinant, -matrix.c / determinant, matrix.a / determinant
    return Matrix(a, b, c, d, -(a * matrix.e + c * matrix.f), -(b * matrix.e + d * matrix.f))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("reference_png must be a PNG image")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _safe_direction(value: Any) -> str:
    direction = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", direction):
        raise ValueError("direction_id must use 1-64 ASCII letters, numbers, underscores, or hyphens")
    return direction


def _browser_svg(root: ET.Element) -> str:
    """Remove active/external content before inserting an SVG into the editor DOM."""
    clone = copy.deepcopy(root)

    def clean(parent: ET.Element) -> None:
        for child in list(parent):
            if local_name(child.tag) in UNSAFE_BROWSER_TAGS:
                parent.remove(child)
                continue
            clean(child)
        for key in list(parent.attrib):
            plain = local_name(key).lower()
            value = parent.attrib[key]
            if plain.startswith("on") or plain in {"href", "src"} and ("://" in value or value.lower().startswith("javascript:")):
                parent.attrib.pop(key, None)

    clean(clone)
    return ET.tostring(clone, encoding="unicode")


class ReviewSession:
    def __init__(self, config_path: Path, *, punchout_entry: str | None = None) -> None:
        self.config_path = config_path.resolve()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.task_id = str(config["task_id"])
        self.reference = self._config_path(config["reference_png"])
        self.source = self._config_path(config["current_svg"])
        self.output_dir = self._config_path(config["output_dir"])
        if not self.reference.is_file() or not self.source.is_file():
            raise FileNotFoundError("reference_png and current_svg must exist")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.output_dir == self.source.parent or self.output_dir == self.reference.parent:
            raise ValueError("output_dir must differ from both input directories")
        self.document = SvgDocument.load(self.source)
        ids = [element.get("id") for element in self.document.root.iter() if element.get("id")]
        if len(ids) != len(set(ids)):
            raise ValueError("The input SVG contains duplicate element IDs")
        self.canvas = self._canvas()
        reference_size = _png_size(self.reference)
        if abs(reference_size[0] - self.canvas[2]) > 1 or abs(reference_size[1] - self.canvas[3]) > 1:
            raise ValueError("Reference PNG dimensions must match the SVG viewBox width and height")
        self.arrow_shield_map = config.get("arrow_shield_map", [])
        self.shield_groups = config.get("shield_groups", [])
        self.arrow_groups = config.get("arrow_groups", [])
        self.font_options = config.get("font_options", [])
        self.locked_ids = set(config.get("locked_ids", []))
        self._lock_canvas_backgrounds()
        self._validate_groups()
        self.punchout_entry = punchout_entry or config.get("punchout_entry")
        self.undo_stack: list[tuple[SvgDocument, dict[str, Any]]] = []
        self.redo_stack: list[tuple[SvgDocument, dict[str, Any]]] = []
        self.history: list[dict[str, Any]] = []
        self.revision = 0
        self.prepared_revision: int | None = None
        self.prepared_files: list[Path] = []
        self.prepared_dir: Path | None = None
        self.finalized_revision: int | None = None
        shutil.copyfile(self.source, self.output_dir / "original.svg")

    def _config_path(self, value: str) -> Path:
        path = Path(value)
        return (path if path.is_absolute() else self.config_path.parent / path).resolve()

    def _canvas(self) -> tuple[float, float, float, float]:
        raw = self.document.root.get("viewBox")
        if not raw:
            raise ValueError("The SVG root must define viewBox")
        values = [float(item) for item in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", raw)]
        if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
            raise ValueError("Invalid SVG viewBox")
        return tuple(values)  # type: ignore[return-value]

    def _validate_groups(self) -> None:
        index = self.document.index()
        directions = set()
        if not isinstance(self.arrow_shield_map, list) or not self.arrow_shield_map:
            raise ValueError("arrow_shield_map must contain at least one direction")
        for entry in self.arrow_shield_map:
            direction = _safe_direction(entry["direction_id"])
            if direction in directions:
                raise ValueError(f"Duplicate direction_id: {direction}")
            directions.add(direction)
            if not entry.get("arrow_path_ids"):
                raise ValueError(f"Direction {direction} has no arrow paths")
            for field in ("arrow_path_ids", "shield_path_ids", "shield_text_ids"):
                for element_id in entry.get(field, []):
                    if element_id not in index:
                        raise ValueError(f"Unknown {field} ID: {element_id}")
        for group_list in (self.shield_groups, self.arrow_groups):
            for group in group_list:
                for element_id in group.get("path_ids", []):
                    if element_id not in index:
                        raise ValueError(f"Unknown group member ID: {element_id}")
        for element_id in self.locked_ids:
            if element_id not in index:
                raise ValueError(f"Unknown locked ID: {element_id}")

    def _lock_canvas_backgrounds(self) -> None:
        x, y, width, height = self.canvas
        for item in self.document.manifest()["elements"]:
            if item["type"] != "rect" or not item.get("bbox"):
                continue
            box = item["bbox"]
            if all(abs(actual - expected) <= 0.5 for actual, expected in zip(box, (x, y, x + width, y + height))):
                self.locked_ids.add(item["id"])

    def _elements(self) -> list[dict[str, Any]]:
        manifest = self.document.manifest()
        result = []
        x, y, width, height = self.canvas
        for item in manifest["elements"]:
            if item["type"] not in EDITABLE:
                continue
            element = self.document.index()[item["id"]][0]
            bbox = item.get("bbox")
            margin = 0.5
            item = {key: value for key, value in item.items() if key not in {"normalized_path", "path_d"}}
            if item["type"] == "text":
                inherited = self._effective_text_properties(item["id"])
                item["font_family"] = inherited.get("font-family")
                item["font_size"] = inherited.get("font-size")
            else:
                item["font_family"] = None
                item["font_size"] = None
            item["text_length"] = element.get("textLength") if item["type"] == "text" else None
            item["text_editable"] = item["type"] == "text" and not list(element)
            item["locked"] = item["id"] in self.locked_ids
            item["out_of_bounds"] = bool(bbox and (bbox[0] < x - margin or bbox[1] < y - margin or bbox[2] > x + width + margin or bbox[3] > y + height + margin))
            item["touches_boundary"] = bool(bbox and (bbox[0] <= x + margin or bbox[1] <= y + margin or bbox[2] >= x + width - margin or bbox[3] >= y + height - margin))
            result.append(item)
        return result

    def _effective_text_properties(self, element_id: str) -> dict[str, str]:
        index = self.document.index()
        chain = []
        element = index[element_id][0]
        while element is not None:
            chain.append(element)
            parent_record = index.get(element.get("id", ""))
            element = parent_record[1] if parent_record else None
        result: dict[str, str] = {}
        for ancestor in reversed(chain):
            for name in INHERITED:
                value = _style_get(ancestor, name)
                if value is not None:
                    result[name] = value
        return result

    def state(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "prepared": self.prepared_revision == self.revision,
            "finalized": self.finalized_revision == self.revision,
            "can_undo": bool(self.undo_stack),
            "can_redo": bool(self.redo_stack),
            "canvas": self.canvas,
            "svg": _browser_svg(self.document.root),
            "elements": self._elements(),
            "arrow_shield_map": self.arrow_shield_map,
            "shield_groups": self.shield_groups,
            "arrow_groups": self.arrow_groups,
            "font_options": self.font_options,
            "history": self.history,
            "punchout_available": bool(self.punchout_entry),
            "prepared_directions": [path.stem for path in self.prepared_files] if self.prepared_revision == self.revision else [],
        }

    def _targets(self, values: Any) -> list[tuple[str, ET.Element, ET.Element | None, Matrix]]:
        if not isinstance(values, list) or not values:
            raise ValueError("Select at least one element")
        index = self.document.index()
        targets = []
        seen = set()
        for raw in values:
            element_id = str(raw)
            if element_id in seen:
                continue
            seen.add(element_id)
            if element_id in self.locked_ids:
                raise ValueError(f"Element {element_id} is locked")
            if element_id not in index:
                raise ValueError(f"Unknown element ID: {element_id}")
            element, parent, parent_matrix, _world, _z = index[element_id]
            if local_name(element.tag) not in EDITABLE:
                raise ValueError(f"Element {element_id} is not directly editable")
            targets.append((element_id, element, parent, parent_matrix))
        return targets

    def apply(self, action: dict[str, Any]) -> dict[str, Any]:
        if self.finalized_revision is not None:
            raise ValueError("This review task is finalized")
        if action.get("revision") != self.revision:
            raise ValueError("The editor state changed; reload before applying another edit")
        candidate = self.document.clone()
        previous = self.document
        self.document = candidate
        try:
            summary = self._apply_mutation(action)
            if set(self.document.index()) != set(previous.index()):
                raise ValueError("An edit changed the element ID set")
        except Exception:
            self.document = previous
            raise
        self.undo_stack.append((previous, summary))
        self.redo_stack.clear()
        self.revision += 1
        self.prepared_revision = None
        self.history.append({"revision": self.revision, **summary})
        return self.state()

    def _apply_mutation(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = action.get("type")
        targets = self._targets(action.get("ids"))
        ids = [item[0] for item in targets]
        if kind == "move":
            dx, dy = _finite(action.get("dx")), _finite(action.get("dy"))
            if dx == 0 and dy == 0:
                raise ValueError("Movement must be nonzero")
            for _element_id, element, _parent, parent_matrix in targets:
                local_dx, local_dy = parent_matrix.inverse_vector(dx, dy)
                old = element.get("transform", "").strip()
                element.set("transform", f"translate({local_dx:.10g} {local_dy:.10g}) {old}".strip())
            return {"type": kind, "ids": ids, "dx": dx, "dy": dy, "summary": f"Moved {len(ids)} element(s) by ({dx:g}, {dy:g})"}
        if len(targets) != 1:
            raise ValueError(f"{kind} requires exactly one selected element")
        element_id, element, parent, _parent_matrix = targets[0]
        if kind == "text":
            if local_name(element.tag) != "text" or list(element):
                raise ValueError("Text editing requires a plain <text> element without tspans")
            changes = action.get("changes", {})
            if not isinstance(changes, dict) or not changes:
                raise ValueError("No text changes were supplied")
            before = {"content": element.text or "", "font_size": _style_get(element, "font-size"), "font_family": _style_get(element, "font-family"), "text_length": element.get("textLength")}
            if "content" in changes:
                if not isinstance(changes["content"], str):
                    raise ValueError("Text content must be a string")
                element.text = changes["content"]
            if "font_size" in changes:
                _style_set(element, "font-size", f"{_finite(changes['font_size'], positive=True):.10g}")
            if "font_family" in changes:
                family = str(changes["font_family"]).strip()
                if not family or len(family) > 200:
                    raise ValueError("A font family name is required")
                _style_set(element, "font-family", family)
            if "text_length" in changes:
                width = changes["text_length"]
                if width is None or width == "":
                    element.attrib.pop("textLength", None)
                    element.attrib.pop("lengthAdjust", None)
                else:
                    element.set("textLength", f"{_finite(width, positive=True):.10g}")
                    element.set("lengthAdjust", "spacingAndGlyphs")
            return {"type": kind, "ids": ids, "before": before, "after": changes, "summary": f"Edited text {element_id}"}
        if kind == "layer":
            if parent is None:
                raise ValueError("The SVG root cannot be reordered")
            direction = action.get("direction")
            siblings = list(parent)
            position = siblings.index(element)
            if direction in {"front", "back"} and parent is not self.document.root:
                self._promote_to_root(element_id, element, parent, direction)
                return {"type": kind, "ids": ids, "direction": direction, "summary": f"Moved {element_id} to the {direction} layer"}
            if direction == "forward":
                new_position = min(position + 1, len(siblings) - 1)
            elif direction == "backward":
                new_position = max(position - 1, 0)
            elif direction in {"front", "back"}:
                new_position = len(siblings) - 1 if direction == "front" else 0
            else:
                raise ValueError("Unknown layer direction")
            if new_position == position:
                raise ValueError("The element is already at that layer position")
            parent.remove(element)
            parent.insert(new_position, element)
            return {"type": kind, "ids": ids, "direction": direction, "summary": f"Moved {element_id} {direction} in layer order"}
        raise ValueError(f"Unsupported review action: {kind}")

    def _promote_to_root(self, element_id: str, element: ET.Element, parent: ET.Element, direction: str) -> None:
        index = self.document.index()
        ancestor = parent
        while ancestor is not self.document.root:
            if any(ancestor.get(name) for name in ("clip-path", "mask", "filter")):
                raise ValueError("This layer is inside a clip, mask, or filter and cannot safely move to the root")
            inherited_opacity = _style_get(ancestor, "opacity")
            if inherited_opacity not in (None, "1", "1.0"):
                raise ValueError("This layer inherits group opacity and cannot safely move to the root")
            record = index.get(ancestor.get("id", ""))
            if record is None or record[1] is None:
                raise ValueError("Cannot resolve the selected layer's parent chain")
            ancestor = record[1]
        effective = self._effective_text_properties(element_id)
        if local_name(element.tag) != "text":
            # Promoting a shape that inherits paint can change appearance. Keep
            # promotion restricted to the common covered-text repair.
            raise ValueError("Cross-group To front/back is supported for text only")
        for name, value in effective.items():
            if _style_get(element, name) is None:
                element.set(name, value)
        world = index[element_id][3]
        root_world = index[self.document.root.get("id")][3] if self.document.root.get("id") in index else Matrix()
        new_transform = _inverse(root_world) @ world
        element.set("transform", _matrix_text(new_transform))
        parent.remove(element)
        self.document.root.insert(len(self.document.root) if direction == "front" else 0, element)

    def undo(self) -> dict[str, Any]:
        if self.finalized_revision is not None:
            raise ValueError("This review task is finalized")
        if not self.undo_stack:
            raise ValueError("Nothing to undo")
        previous, event = self.undo_stack.pop()
        self.redo_stack.append((self.document, event))
        self.document = previous
        self.revision += 1
        self.prepared_revision = None
        self.history.append({"revision": self.revision, "type": "undo", "summary": f"Undid: {event['summary']}"})
        return self.state()

    def redo(self) -> dict[str, Any]:
        if self.finalized_revision is not None:
            raise ValueError("This review task is finalized")
        if not self.redo_stack:
            raise ValueError("Nothing to redo")
        next_document, event = self.redo_stack.pop()
        self.undo_stack.append((self.document, event))
        self.document = next_document
        self.revision += 1
        self.prepared_revision = None
        self.history.append({"revision": self.revision, "type": "redo", "summary": f"Redid: {event['summary']}"})
        return self.state()

    def save_draft(self) -> Path:
        path = self.output_dir / "draft.svg"
        self.document.save(path)
        return path

    def prepare(self) -> dict[str, Any]:
        if self.finalized_revision is not None:
            raise ValueError("This review task is finalized")
        if not self.punchout_entry:
            raise ValueError("Configure punchout_entry as module:function before preparing direction outputs")
        module_name, separator, function_name = self.punchout_entry.partition(":")
        if not separator or not module_name or not function_name:
            raise ValueError("punchout_entry must use module:function")
        adapter = getattr(importlib.import_module(module_name), function_name)
        prepared_dir = self.output_dir / "prepared" / f"revision_{self.revision:04d}"
        prepared_dir.mkdir(parents=True, exist_ok=True)
        candidate = prepared_dir / "edited.svg"
        self.document.save(candidate)
        render_svg(candidate, prepared_dir / "edited.png")
        directions_dir = prepared_dir / "directions"
        directions_dir.mkdir(exist_ok=True)
        returned = adapter(candidate, copy.deepcopy(self.arrow_shield_map), directions_dir)
        if not isinstance(returned, (list, tuple)):
            raise ValueError("Punch-out adapter must return a list of direction SVG paths")
        files = [(Path(path) if Path(path).is_absolute() else directions_dir / Path(path)).resolve() for path in returned]
        expected = {_safe_direction(entry["direction_id"]) for entry in self.arrow_shield_map}
        if len(files) != len(expected) or {path.stem for path in files} != expected:
            raise ValueError("Punch-out output must contain one <direction_id>.svg per mapping entry")
        for path in files:
            if path.parent != directions_dir.resolve() or not path.is_file() or path.suffix.lower() != ".svg":
                raise ValueError("Punch-out adapter returned an invalid output path")
            ET.parse(path)
        self.prepared_dir = prepared_dir
        self.prepared_files = files
        self.prepared_revision = self.revision
        return self.state()

    def finalize(self) -> dict[str, Any]:
        if self.prepared_revision != self.revision or self.prepared_dir is None:
            raise ValueError("Prepare and inspect direction outputs for the current revision first")
        edited = self.output_dir / "edited.svg"
        image = self.output_dir / "edited.png"
        shutil.copyfile(self.prepared_dir / "edited.svg", edited)
        shutil.copyfile(self.prepared_dir / "edited.png", image)
        directions = self.output_dir / "directions"
        directions.mkdir(exist_ok=True)
        for source in self.prepared_files:
            shutil.copyfile(source, directions / source.name)
        log = {
            "task_id": self.task_id,
            "revision": self.revision,
            "original_sha256": _sha256(self.output_dir / "original.svg"),
            "edited_sha256": _sha256(edited),
            "directions": [path.name for path in self.prepared_files],
            "operations": self.history,
        }
        (self.output_dir / "edits.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        self.finalized_revision = self.revision
        return {"output_dir": str(self.output_dir), "edited_svg": str(edited), "directions": [str(directions / path.name) for path in self.prepared_files]}
