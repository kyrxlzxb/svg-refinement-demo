from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from prompts import ARROW_VALIDATION_PROMPT, SYSTEM_PROMPT, build_analysis_prompt


class BedrockAnalysisError(RuntimeError):
    """Raised when Bedrock does not return a usable structured repair plan."""


def _image_format(path: Path) -> str:
    suffix = path.suffix.lower()
    formats = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}
    try:
        return formats[suffix]
    except KeyError as exc:
        raise ValueError(f"Unsupported image format: {path}") from exc


def _extract_json(text: str, *, require_issues: bool = True) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise BedrockAnalysisError(f"Model response was not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BedrockAnalysisError("Model response must be a JSON object")
    if require_issues and not isinstance(value.get("issues"), list):
        raise BedrockAnalysisError("Model response must be an object containing an issues array")
    if require_issues:
        value.setdefault("summary", "")
    return value


class BedrockAnalyzer:
    def __init__(self, *, region: str, model_id: str, api_key: str | None = None) -> None:
        if api_key:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = api_key
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required; install requirements.txt") from exc
        self._client = boto3.client("bedrock-runtime", region_name=region)
        self._model_id = model_id

    def _converse(self, *, content: list[dict[str, Any]], system_prompt: str) -> str:
        response = self._client.converse(
            modelId=self._model_id,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": 4096, "temperature": 0.0},
        )
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "\n".join(block.get("text", "") for block in blocks if "text" in block)
        if not text.strip():
            raise BedrockAnalysisError("Bedrock response contained no text")
        return text

    def analyze(
        self,
        *,
        reference_path: Path,
        rendered_path: Path,
        element_atlas_path: Path,
        manifest: dict[str, Any],
        repair_history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        content = [
            {
                "image": {
                    "format": _image_format(reference_path),
                    "source": {"bytes": reference_path.read_bytes()},
                }
            },
            {
                "image": {
                    "format": _image_format(rendered_path),
                    "source": {"bytes": rendered_path.read_bytes()},
                }
            },
            {
                "image": {
                    "format": _image_format(element_atlas_path),
                    "source": {"bytes": element_atlas_path.read_bytes()},
                }
            },
            {
                "text": build_analysis_prompt(
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(repair_history, ensure_ascii=False, separators=(",", ":")),
                )
            },
        ]
        return _extract_json(self._converse(content=content, system_prompt=SYSTEM_PROMPT))

    def validate_arrow(
        self,
        *,
        reference_path: Path,
        before_path: Path,
        candidate_path: Path,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        content = []
        for path in (reference_path, before_path, candidate_path):
            content.append(
                {"image": {"format": _image_format(path), "source": {"bytes": path.read_bytes()}}}
            )
        content.append({"text": ARROW_VALIDATION_PROMPT + "\nAttempted action:\n" + json.dumps(action, ensure_ascii=False)})
        result = _extract_json(
            self._converse(content=content, system_prompt="You are a strict visual validator for local SVG arrow repairs."),
            require_issues=False,
        )
        required = ("accept", "target_issue_fixed", "new_deformation", "moved_away_from_reference", "reason")
        if any(key not in result for key in required):
            raise BedrockAnalysisError("Arrow validation response is missing required fields")
        if not all(isinstance(result[key], bool) for key in required[:-1]) or not isinstance(result["reason"], str):
            raise BedrockAnalysisError("Arrow validation response has invalid field types")
        result["accept"] = bool(
            result["accept"]
            and result["target_issue_fixed"]
            and not result["new_deformation"]
            and not result["moved_away_from_reference"]
        )
        return result
