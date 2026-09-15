from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from bedrock_client import BedrockAnalyzer
from svg_tools import SvgDocument, render_element_atlas, render_svg


PRIORITY = {
    "REPLACE_TEXT": 0,
    "REORDER_ELEMENTS": 1,
    "ALIGN_COMPOSITE_CENTER": 2,
    "REPAIR_ARROW_INTERNAL": 3,
    "ALIGN_ARROW_GROUP": 4,
    "FIT_TEXT_WIDTH": 5,
}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def choose_action(plan: dict[str, Any], min_confidence: float) -> tuple[dict[str, Any] | None, str]:
    candidates = []
    for position, issue in enumerate(plan.get("issues", [])):
        if not isinstance(issue, dict):
            continue
        operation = issue.get("type")
        try:
            confidence = float(issue.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        if operation == "REPAIR_ARROW_INTERNAL":
            try:
                landmark_confidence = float(issue.get("landmark_confidence", 0))
            except (TypeError, ValueError):
                continue
            if landmark_confidence < min_confidence:
                continue
        if operation in PRIORITY and confidence >= min_confidence:
            candidates.append((PRIORITY[operation], position, -confidence, issue))
    if not candidates:
        return None, "No supported issue met the confidence threshold"
    candidates.sort(key=lambda item: item[:3])
    return candidates[0][3], "Selected highest-priority safe supported issue"


def resolve_setting(cli_value: str | None, *environment_names: str) -> str | None:
    if cli_value:
        return cli_value
    for name in environment_names:
        value = os.getenv(name)
        if value:
            return value
    return None


def run(args: argparse.Namespace) -> int:
    reference = args.reference.resolve()
    source_svg = args.svg.resolve()
    output_dir = args.output_dir.resolve()
    if not reference.is_file():
        raise FileNotFoundError(f"Reference image not found: {reference}")
    if not source_svg.is_file():
        raise FileNotFoundError(f"Input SVG not found: {source_svg}")
    if output_dir in {reference.parent, source_svg.parent}:
        raise ValueError("Use a separate output directory so input files cannot be overwritten")

    region = resolve_setting(args.region, "AWS_REGION", "AWS_DEFAULT_REGION")
    model_id = resolve_setting(args.model_id, "BEDROCK_MODEL_ID")
    api_key = resolve_setting(args.api_key, "AWS_BEARER_TOKEN_BEDROCK")
    if not region or not model_id:
        raise ValueError("Provide Region and model with CLI arguments or AWS_REGION and BEDROCK_MODEL_ID")

    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_svg, output_dir / "source_original.svg")
    document = SvgDocument.load(source_svg)
    current_svg = output_dir / "iteration_00_normalized.svg"
    current_png = output_dir / "iteration_00_render.png"
    document.save(current_svg)
    render_svg(current_svg, current_png)
    current_atlas_svg = output_dir / "iteration_00_element_atlas.svg"
    current_atlas_png = output_dir / "iteration_00_element_atlas.png"
    current_atlas_metadata = render_element_atlas(document, current_atlas_svg, current_atlas_png)
    initial_manifest = document.manifest()
    initial_manifest["element_atlas"] = current_atlas_metadata
    write_json(output_dir / "iteration_00_manifest.json", initial_manifest)

    analyzer = BedrockAnalyzer(region=region, model_id=model_id, api_key=api_key)
    history: list[dict[str, Any]] = []
    stop_reason = "Iteration limit reached"

    for iteration in range(args.max_iterations):
        manifest = document.manifest()
        manifest["element_atlas"] = current_atlas_metadata
        plan = analyzer.analyze(
            reference_path=reference,
            rendered_path=current_png,
            element_atlas_path=current_atlas_png,
            manifest=manifest,
            repair_history=history,
        )
        write_json(output_dir / f"iteration_{iteration:02d}_plan.json", plan)
        action, selection_reason = choose_action(plan, args.min_confidence)
        if action is None:
            stop_reason = selection_reason
            break

        history_item: dict[str, Any] = {
            "iteration": iteration + 1,
            "requested_action": action,
            "selection_reason": selection_reason,
        }
        candidate = document.clone()
        try:
            history_item["applied"] = candidate.apply_action(action)
        except (TypeError, ValueError) as exc:
            history_item["status"] = "skipped"
            history_item["error"] = str(exc)
            history.append(history_item)
            continue

        operation = str(action["type"]).lower()
        candidate_svg = output_dir / f"iteration_{iteration + 1:02d}_{operation}.svg"
        candidate_png = output_dir / f"iteration_{iteration + 1:02d}_{operation}.png"
        candidate_atlas_svg = output_dir / f"iteration_{iteration + 1:02d}_element_atlas.svg"
        candidate_atlas_png = output_dir / f"iteration_{iteration + 1:02d}_element_atlas.png"
        try:
            candidate.save(candidate_svg)
            render_svg(candidate_svg, candidate_png)
            candidate_atlas_metadata = render_element_atlas(candidate, candidate_atlas_svg, candidate_atlas_png)
        except Exception as exc:
            history_item["status"] = "reverted"
            history_item["render_error"] = str(exc)
            history.append(history_item)
            continue

        if action["type"] in {"REPAIR_ARROW_INTERNAL", "ALIGN_ARROW_GROUP"}:
            try:
                validation = analyzer.validate_arrow(
                    reference_path=reference,
                    before_path=current_png,
                    candidate_path=candidate_png,
                    action=action,
                )
            except Exception as exc:
                history_item["status"] = "reverted"
                history_item["validation_error"] = str(exc)
                history.append(history_item)
                stop_reason = f"Arrow validation failed; candidate was preserved but reverted: {exc}"
                break
            write_json(output_dir / f"iteration_{iteration + 1:02d}_arrow_validation.json", validation)
            history_item["validation"] = validation
            if not validation["accept"]:
                history_item["status"] = "reverted"
                history.append(history_item)
                continue

        history_item["status"] = "applied"
        history.append(history_item)
        document = candidate
        current_svg, current_png = candidate_svg, candidate_png
        current_atlas_svg, current_atlas_png = candidate_atlas_svg, candidate_atlas_png
        current_atlas_metadata = candidate_atlas_metadata
        accepted_manifest = document.manifest()
        accepted_manifest["element_atlas"] = current_atlas_metadata
        write_json(output_dir / f"iteration_{iteration + 1:02d}_manifest.json", accepted_manifest)

    shutil.copyfile(current_svg, output_dir / "final.svg")
    shutil.copyfile(current_png, output_dir / "final.png")
    result = {
        "source_svg": str(source_svg),
        "reference_image": str(reference),
        "region": region,
        "model_id": model_id,
        "repairs": history,
        "stop_reason": stop_reason,
        "final_svg": str(output_dir / "final.svg"),
        "final_render": str(output_dir / "final.png"),
    }
    write_json(output_dir / "history.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refine a mostly-correct SVG through Amazon Bedrock")
    parser.add_argument("--reference", type=Path, required=True, help="Reference PNG/JPEG/WebP image")
    parser.add_argument("--svg", type=Path, required=True, help="Existing SVG to refine (never modified)")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for snapshots and final output")
    parser.add_argument("--region", help="Amazon Bedrock region; otherwise AWS_REGION/AWS_DEFAULT_REGION")
    parser.add_argument("--model-id", help="Bedrock model or inference-profile ID; otherwise BEDROCK_MODEL_ID")
    parser.add_argument("--api-key", help="Bedrock API key; otherwise AWS_BEARER_TOKEN_BEDROCK")
    parser.add_argument("--max-iterations", type=int, default=4)
    parser.add_argument("--min-confidence", type=float, default=0.65)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.max_iterations < 1 or args.max_iterations > 10:
        parser.error("--max-iterations must be between 1 and 10")
    if not 0 <= args.min_confidence <= 1:
        parser.error("--min-confidence must be between 0 and 1")
    try:
        return run(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
