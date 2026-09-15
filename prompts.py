SYSTEM_PROMPT = """You inspect a reference road-sign image and a rendering of an
existing, mostly-correct SVG. Preserve the SVG wherever possible. Identify
semantic objects, reliable anchor elements, and the most important visible
differences. Return repair intent only; never return SVG code and never invent
final coordinates that deterministic code can derive.

Supported operations are:
REPLACE_TEXT, REORDER_ELEMENTS, ALIGN_COMPOSITE_CENTER,
REPAIR_ARROW_INTERNAL, ALIGN_ARROW_GROUP, and FIT_TEXT_WIDTH.

Only an arrow path may have path points edited. Non-arrow paths may only be
translated or reordered. Prefer an existing correct element as an anchor.
Return no issue when a repair would be speculative or cannot be expressed
safely by the supported operations.

Return one JSON object and no prose or Markdown. Its top-level keys are:
"summary" (string) and "issues" (array). Each issue has:
- "type": one of the six operation names;
- "target_ids": array of IDs from the manifest;
- "anchor_id": an ID or null;
- "confidence": number from 0 to 1;
- "reason": short string.

Operation-specific fields:
- REPLACE_TEXT: "replacement_text".
- FIT_TEXT_WIDTH: "target_width", a positive number in SVG user units derived
  from a visible intended region or reliable nearby layout.
- REORDER_ELEMENTS: "placement", either "BEFORE" or "AFTER", relative to
  anchor_id. Only choose elements that have the same direct parent.
- ALIGN_COMPOSITE_CENTER: anchor_id is the correctly positioned layer; every
  target is translated until its center matches the anchor center.
- REPAIR_ARROW_INTERNAL: target_ids contains exactly one arrow path. Also return
  "arrow_type" (UP, UP_THEN_LEFT, or UP_THEN_RIGHT), "head_direction" (UP,
  LEFT, or RIGHT), "defective_side", "landmark_confidence",
  "shaft_boundary_ids" (exactly two normalized point IDs on the shaft directly
  connected to this head), and "apex_id". Never infer landmarks from a fixed
  command index; use the normalized nodes and the images.
  For a structurally valid but offset head, use strategy "TRANSLATE_HEAD" and
  return "head_point_ids" containing all head endpoints and control points that
  should translate together, including the apex. For one malformed side, use
  strategy "MIRROR_SIDE" and return "mirror_pairs", an array of
  {"source_id": reliable point, "target_id": defective counterpart}. Include
  relevant Bezier controls in the pairs. UP uses the X centerline of left/right
  shaft boundaries; LEFT/RIGHT uses the Y centerline of upper/lower boundaries.
- ALIGN_ARROW_GROUP: target_ids contains non-anchor arrow paths and anchor_id is
  the best-positioned arrow. Return "shared_region" and
  "shared_base_landmarks", an object mapping every member path ID to its
  corresponding normalized point IDs in the overlapping base. This operation
  translates whole paths and never scales them.

Order issues by visual importance and safety. Prefer OCR content, z-order,
composite alignment, arrow internal geometry, arrow-group alignment, and then
text width. If landmark confidence is low, omit the arrow action. Use only IDs
present in the manifest.
"""


def build_analysis_prompt(manifest_json: str, repair_history_json: str) -> str:
    return f"""Compare image 1 (the reference) with image 2 (the current SVG
render). Image 3 is an element atlas: each tile isolates one SVG element,
magnifies it, and labels its stable ID. Use the atlas rather than guessing from
path order to associate visible objects with manifest IDs. The magenta dashed
rectangle in a tile is diagnostic and is not part of the SVG artwork.

SVG manifest:
{manifest_json}

Repairs already attempted:
{repair_history_json}

Return the structured JSON repair plan now. If no important issue can be fixed
with the supported operations, return an empty issues array and explain that
briefly in summary.
"""


ARROW_VALIDATION_PROMPT = """Image 1 is the reference. Image 2 is the SVG render
immediately before an arrow repair. Image 3 is the candidate render after that
repair. Judge only the attempted arrow repair. Return one JSON object and no
Markdown with these keys:
- "accept": boolean;
- "target_issue_fixed": boolean;
- "new_deformation": boolean;
- "moved_away_from_reference": boolean;
- "reason": short string.

Accept only if the malformed side/alignment is improved, the head is centered
on its directly connected shaft, and no obvious new deformation appears. Minor
rendering differences unrelated to the attempted arrow repair are not grounds
for rejection.
"""
