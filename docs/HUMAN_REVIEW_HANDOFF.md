# Human Review Stage: Integration Handoff

This document describes the implemented editor, its pipeline contract, and the
one adapter required to connect an existing punch-out function. The editor
starts after automatic SVG alignment and before directional arrow extraction.

## 1. Workflow

```text
Reference PNG + automatically refined SVG + arrow/Shield ID mapping
  -> human layout review
  -> operator finishes layout
  -> current SVG is passed to the punch-out adapter
  -> operator checks every direction output and overlay
  -> operator confirms final files
```

The editor does not call a foundation model for manual moves. The selected
elements retain their SVG IDs. Numeric movement comes from the controls or a
drag gesture and is converted into SVG viewBox units.

## 2. Launch contract

Run:

```bash
python review_app.py --config review-task.json --punchout your_module:run_punchout
```

The local page is available at `http://127.0.0.1:8765`. `--host` and `--port`
are optional. `punchout_entry` may instead be placed in the JSON config.
An existing Python Web process may create `ReviewSession(config_path)` from
`review_core.py` and pass it to `create_app(session)` in `review_app.py` rather
than launching a second process. The current implementation holds one review
task per `ReviewSession` instance.

Required config fields:

| Field | Meaning |
|---|---|
| `task_id` | Identifier shown in the editor and final log |
| `reference_png` | Reference image; must have the same pixel width and height as the SVG viewBox |
| `current_svg` | Complete SVG at the end of automatic refinement |
| `output_dir` | Dedicated output directory for this review task |
| `arrow_shield_map` | One entry per direction, containing arrow and related Shield IDs |

Optional fields: `shield_groups`, `arrow_groups`, `font_options`, `locked_ids`,
and `punchout_entry`. Relative paths resolve from the JSON file directory.

Example:

```json
{
  "task_id": "sign-001",
  "reference_png": "reference.png",
  "current_svg": "aligned.svg",
  "output_dir": "review-output",
  "arrow_shield_map": [
    {
      "direction_id": "up",
      "arrow_path_ids": ["path_021"],
      "shield_path_ids": ["path_031", "path_032", "path_033"],
      "shield_text_ids": ["text_008"]
    },
    {
      "direction_id": "right",
      "arrow_path_ids": ["path_022"],
      "shield_path_ids": ["path_041", "path_042"],
      "shield_text_ids": ["text_009"]
    }
  ],
  "shield_groups": [
    {"group_id": "up Shield frame", "path_ids": ["path_031", "path_032", "path_033"]}
  ],
  "arrow_groups": [
    {"group_id": "shared arrow base", "path_ids": ["path_021", "path_022"]}
  ],
  "font_options": ["Noto Sans CJK JP", "Arial"],
  "locked_ids": ["rect_001"],
  "punchout_entry": "your_module:run_punchout"
}
```

`direction_id` is an output filename stem and may contain only ASCII letters,
digits, `_`, and `-`. Every ID in the mapping and suggested groups must exist
in `current_svg`. The loader checks for duplicate SVG IDs. The editor keeps
the ID set unchanged during edits and rejects an action that changes it.

The reference and SVG currently require matching canvas dimensions and no
crop offset. Register the same font families in the browser and the backend
renderer before relying on text-width comparisons. The browser gives instant
feedback; the backend-rendered image shown below the editor is the final
rendering check.

## 3. Selection and editing behavior

The first screen shows the reference PNG, the SVG in its normal colors, an
outline view, and a compact inspector. Selecting an element updates all three
views and a separate selected-object preview. The projection on the reference
shows the **current SVG position**; it does not attempt to identify the target
object in the PNG.

Clicking any mapped arrow selects all directional arrows and their mapped
Shield frames as one movement set. Clicking a Shield frame selects its mapped
frame layers, while clicking its text selects only that text. The operator can
add or remove individual elements through the overlap candidate menu and can
save a selection preset for the current page session. The interface does not
require semantic names for every path.

Implemented operations:

- Move one or several elements by button, arrow key, or dragging their
  projection on the reference. The default step is one SVG viewBox unit and
  can be changed. Horizontal or vertical movement can be locked.
- Edit plain SVG `<text>` content, font size, horizontal `textLength`, and
  font-family name. Outlined text paths remain movable but cannot be edited as
  text. Complex text with child `<tspan>` is not rewritten by this editor.
- Move one element forward/backward among siblings, or to front/back. Nested
  plain text can be promoted to the root to clear a covering panel; this is
  refused when a clip, mask, or filter makes the promotion unsafe. Cross-group
  promotion of arbitrary shapes is not supported.
- Undo and redo; save an editable `draft.svg` at any time.
- Show approximate out-of-bounds and edge-contact warnings. The manifest uses
  estimated text bounding boxes; the operator should inspect the actual
  render before final confirmation.

The editor serves a sanitized SVG copy to the browser to avoid executing
embedded scripts. The editing source remains the parsed SVG document.

## 4. Punch-out adapter contract

The editor calls a Python function identified by `module:function`:

```python
def run_punchout(
    edited_svg_path: pathlib.Path,
    arrow_shield_map: list[dict],
    output_dir: pathlib.Path,
) -> list[pathlib.Path]:
    """Write one <direction_id>.svg per mapping entry and return those paths."""
```

The function receives the **current edited SVG**, the ID mapping, and an
already created direction output directory. It may call an existing function
that reads the whole SVG, or convert the mapping to the existing function's
expected objects. It must return exactly one valid SVG file for each direction.
Returned paths may be absolute or relative to `output_dir`. The editor
validates returned paths and names before marking the outputs
current. The repository does not contain the specific punch-out algorithm,
because its existing input signature and masking implementation are not part
of this project.

Clicking **Finish layout · split arrows** creates an immutable preparation
snapshot under `prepared/revision_N/`, renders it with CairoSVG, calls the
adapter, and shows each direction SVG beside an overlay on the complete sign.
Any later edit or undo marks those outputs stale. The operator must prepare
again before **Confirm final outputs** becomes available.

## 5. Output contract

```text
output_dir/
  original.svg                  Exact input SVG copy
  draft.svg                     Optional in-progress snapshot
  prepared/revision_N/
    edited.svg                  Snapshot passed to punch-out
    edited.png                  Backend render of that snapshot
    directions/<direction_id>.svg
  edited.svg                    Confirmed complete SVG
  edited.png                    Confirmed backend render
  directions/<direction_id>.svg Confirmed directional outputs
  edits.json                    Task ID, revision, hashes, and operation log
```

The caller should treat `edited.svg` and `directions/` as final only after the
operator clicks **Confirm final outputs**. The original input SVG is not
overwritten. The `edits.json` log records IDs, operation types, movement
numbers, text changes, layer changes, and undo/redo steps.
Final confirmation locks the current review session. Further changes require
starting a new review task from the desired SVG snapshot.

## 6. Integration points and checks

1. Export the existing arrow-to-Shield association as `arrow_shield_map` with
   stable IDs. Suggested Shield and arrow groups are optional.
2. Wrap the existing punch-out function with the adapter signature above.
   Verify it reads the edited preparation snapshot, not a prior SVG.
3. Provide the reference PNG, current SVG, output path, and installed fonts.
4. Check selection of covered text, simultaneous movement of Shield layers,
   text width changes, cross-group text stacking, edge warnings, undo/redo,
   and output invalidation after a new edit.
5. Compare `edited.png` with the browser preview, then inspect every isolated
   direction SVG and its overlay before final confirmation.
