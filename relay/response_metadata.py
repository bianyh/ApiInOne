from __future__ import annotations

from typing import Any, BinaryIO

import ijson


_USAGE_FIELDS = {
    "input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "total_tokens",
    "input_tokens_details.cached_tokens", "prompt_tokens_details.cached_tokens",
    "output_tokens_details.reasoning_tokens", "completion_tokens_details.reasoning_tokens",
}


def read_response_metadata(source: BinaryIO) -> dict[str, Any]:
    """Extract routing and usage fields from a large JSON response or SSE event."""
    metadata: dict[str, Any] = {}
    objects: list[dict[str, str]] = []
    frames: list[tuple[str, dict[str, str]]] = []
    try:
        for prefix, event, value in ijson.parse(source, use_float=True):
            if event == "start_map":
                frames.append(("", {}))
                if prefix == "error":
                    metadata["error"] = {}
            elif event == "end_map":
                _, fields = frames.pop()
                if fields.get("id"):
                    objects.append(fields)
            elif event == "map_key":
                frames[-1] = (value, frames[-1][1])
            elif event == "string":
                if prefix in ("type", "service_tier") and len(value) <= 128:
                    metadata[prefix] = value
                key, fields = frames[-1] if frames else ("", {})
                if key in ("id", "object") and prefix.rsplit(".", 1)[-1] == key and len(value) <= 8192:
                    fields[key] = value
            elif event == "number":
                if prefix.startswith("usage.") and prefix[6:] in _USAGE_FIELDS:
                    target = metadata.setdefault("usage", {})
                    field = prefix[6:]
                elif prefix.startswith("response.usage.") and prefix[15:] in _USAGE_FIELDS:
                    target = metadata.setdefault("response", {}).setdefault("usage", {})
                    field = prefix[15:]
                else:
                    continue
                if "." in field:
                    group, field = field.split(".", 1)
                    target = target.setdefault(group, {})
                target[field] = value
    except (ijson.JSONError, ValueError, OverflowError):
        # The original bytes are still relayed when upstream JSON cannot be inspected.
        pass
    if objects:
        metadata["objects"] = objects
    return metadata
