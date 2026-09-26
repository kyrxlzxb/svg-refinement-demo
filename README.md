# SVG Refinement Demo

A small Python prototype for repairing mostly-correct road-sign SVGs. The
multimodal model decides **what is wrong**; deterministic Python code decides
**how to edit the SVG**.

The prototype supports:

- stable SVG element IDs and a compact manifest;
- SVG-to-PNG rendering;
- a labeled element atlas that isolates and magnifies each path/text/shape so
  the model can map visual objects back to stable IDs;
- multimodal comparison through Amazon Bedrock;
- `REPLACE_TEXT`, `REORDER_ELEMENTS`, `ALIGN_COMPOSITE_CENTER`,
  `REPAIR_ARROW_INTERNAL`, `ALIGN_ARROW_GROUP`, and `FIT_TEXT_WIDTH`;
- absolute path normalization with stable node, segment, and Bezier-control IDs;
- straight-up and up-then-left/right arrow-head repair by local translation or
  one-sided mirroring;
- shared-base alignment for multi-arrow groups;
- deterministic arrow safety checks and visual accept/revert validation;
- a short analyze -> repair -> render loop with inspectable snapshots.

Only arrow paths may have local path geometry edited. No non-arrow path
geometry is regenerated or edited.

## Requirements

- Python 3.11 or newer
- A Bedrock model that supports images through the Converse API
- An Amazon Bedrock API key, or normal AWS SDK credentials

Install dependencies in the runtime environment:

```bash
python -m pip install -r requirements.txt
```

## Bedrock configuration

Do not commit credentials. Copy `.env.example` to your own environment setup,
then provide these values:

```bash
export AWS_BEARER_TOKEN_BEDROCK="your-bedrock-api-key"
export AWS_REGION="your-region"
export BEDROCK_MODEL_ID="your-multimodal-model-or-inference-profile-id"
```

On PowerShell:

```powershell
$env:AWS_BEARER_TOKEN_BEDROCK = "your-bedrock-api-key"
$env:AWS_REGION = "your-region"
$env:BEDROCK_MODEL_ID = "your-multimodal-model-or-inference-profile-id"
```

`AWS_BEARER_TOKEN_BEDROCK` is the environment variable documented by AWS for
Bedrock API-key authentication. If it is omitted, Boto3 uses its normal AWS
credential chain (for example, an IAM role or an AWS profile).

Region and model can instead be passed with `--region` and `--model-id`. The
API key can be passed with `--api-key`, but the environment variable is safer
because command-line arguments may be stored in shell history.

## Run

```bash
python main.py \
  --reference path/to/reference.png \
  --svg path/to/input.svg \
  --output-dir path/to/run-output
```

Use a dedicated output directory rather than the directory containing either
input. This prevents snapshot names from ever overwriting source material.

Optional arguments:

```text
--region REGION            Overrides AWS_REGION / AWS_DEFAULT_REGION
--model-id MODEL_ID        Overrides BEDROCK_MODEL_ID
--api-key API_KEY          Overrides AWS_BEARER_TOKEN_BEDROCK
--max-iterations N         Maximum repair passes (default: 4)
--min-confidence VALUE     Skip speculative actions (default: 0.65)
```

The output directory contains an exact `source_original.svg` copy, the
stable-ID `iteration_00_normalized.svg`, every repair plan, every intermediate
SVG and PNG, the manifests, `history.json`, and the final `final.svg` /
`final.png` pair. The source SVG is never modified.

The external inputs are one reference image and one SVG. Before every model
analysis, the program renders the current SVG to PNG and builds an element
atlas. Bedrock therefore sees three images: the reference, the current SVG
render, and the labeled element atlas, plus the manifest and repair history.

## Human layout review

The local review editor opens after the automatic alignment stage. It shows
the reference PNG, the normally rendered SVG, and a selectable outline view
on one screen. The editor supports simultaneous movement of selected layers,
text content/font/size/width changes, layer order, edge warnings, and
undo/redo. It projects the selected SVG geometry onto the reference image so
an operator can drag it toward the intended position.

Copy [`review-task.example.json`](review-task.example.json), replace its paths
and IDs, and follow the contract in
[`docs/HUMAN_REVIEW_HANDOFF.md`](docs/HUMAN_REVIEW_HANDOFF.md). Then launch:

```bash
python review_app.py --config review-task.json --punchout your_module:run_punchout
```

Open `http://127.0.0.1:8765`. The editor accepts the original reference PNG,
the latest complete SVG, and the existing arrow-to-Shield ID mapping. The
punch-out adapter must write one `<direction_id>.svg` per mapping entry. Its
signature and the output layout are documented in the handoff. Without an
adapter, layout edits and `draft.svg` saving work, while the final direction
generation control remains disabled.

## Repair-plan contract

The model returns JSON with this shape:

```json
{
  "summary": "Short assessment",
  "issues": [
    {
      "type": "ALIGN_COMPOSITE_CENTER",
      "target_ids": ["path_002", "path_003"],
      "anchor_id": "path_001",
      "confidence": 0.94,
      "reason": "The inner shield layers are offset from the outer layer."
    }
  ]
}
```

Operation-specific fields:

- `REPLACE_TEXT`: `target_ids` must contain one text ID and `replacement_text`
  must be present.
- `FIT_TEXT_WIDTH`: `target_ids` must contain one text ID and
  `target_width` must be a positive SVG-unit width.
- `ALIGN_COMPOSITE_CENTER`: `anchor_id` and one or more `target_ids` are
  required. Only translations are applied.
- `REORDER_ELEMENTS`: `target_ids` contains the elements to move,
  `anchor_id` identifies a sibling, and `placement` is `BEFORE` or `AFTER`.
- `REPAIR_ARROW_INTERNAL`: one target arrow path, `arrow_type`,
  `head_direction`, two `shaft_boundary_ids`, `apex_id`, and either:
  - `strategy: "TRANSLATE_HEAD"` with `head_point_ids`; or
  - `strategy: "MIRROR_SIDE"` with semantic `mirror_pairs`.
- `ALIGN_ARROW_GROUP`: non-anchor paths in `target_ids`, the best-positioned
  path in `anchor_id`, and corresponding point IDs in
  `shared_base_landmarks`.

Only the highest-priority safe action is applied per iteration. Invalid,
low-confidence, unsupported, or non-local actions are recorded and skipped.
An arrow candidate is rendered separately and compared with the reference and
pre-repair render. A failed visual validation is recorded and automatically
reverted; the rejected candidate files remain available for inspection.

## Known limitations

- Text and path bounding boxes in the manifest are practical estimates; the
  model should use the rendered image as visual truth.
- Reordering is limited to elements with the same direct SVG parent.
- Center alignment is translation-only and never edits path data.
- `FIT_TEXT_WIDTH` uses SVG `textLength` with `spacingAndGlyphs`, leaving font
  selection to the renderer.
- Arrow repair supports heads facing only up, left, or right. Arbitrary-angle
  arrows, rotated/skewed path transforms, discontinuous paths, and paths over
  the demo safety limit are left unchanged.
- Semantic landmark mapping remains a multimodal-model responsibility. Code
  validates the mapping, shaft width, displacement, closure, and bounding-box
  change before accepting geometry.
- This repository contains no provider abstraction, UI, database, workflow
  framework, or production infrastructure.

## Smoke check

After configuring Bedrock, run one real pair and verify:

1. the rendered `iteration_00_render.png` matches the input SVG;
2. `iteration_00_element_atlas.png` shows isolated, readable, correctly labeled
   elements;
3. `iteration_00_manifest.json` contains stable IDs visible in the SVG;
4. `iteration_00_plan.json` contains valid element associations;
5. at least one supported repair creates an updated SVG and PNG;
6. an arrow repair produces `arrow_validation.json`, and a rejected candidate
   leaves the prior SVG as the current state;
7. `final.svg` parses and visually improves the selected issue.

## Design documents

The original discussion documents are preserved unchanged under `docs/`:

- `SVG Refinement Demo — Coding Agent Plan.md`
- `SVG Refinement — Implementation Plan.md`
- the original Japanese workflow memo

They are design references, not runtime instructions or executable inputs.

The human review editor and its pipeline handoff are described in
`docs/HUMAN_REVIEW_HANDOFF.md`. Run it with `python review_app.py --config
review-task.json --punchout your_module:run_punchout`. The review interface
is implemented; the existing direction punch-out function must be connected
through the documented adapter signature.
