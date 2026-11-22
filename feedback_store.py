"""Opt-in local feedback records for manually reviewed Danbooru tags.

No image pixels, image paths, or API credentials are stored in the feedback log.
The application never uploads this file.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


BUCKETS = ("high", "medium", "low")


def parse_edited_tags(outputs):
    """Read editable confidence buckets, preserving order without duplicates."""
    tags = []
    seen = set()
    for bucket in BUCKETS:
        for tag in str(outputs.get(bucket, "")).split():
            if tag not in seen:
                tags.append(tag)
                seen.add(tag)
    return tags


def parse_tag_text(text):
    """Parse one flat whitespace-separated final tag list, without duplicates."""
    return list(dict.fromkeys(str(text).split()))


def image_sha256(path):
    """Use image content, not the potentially sensitive local file name."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_feedback_record(
    image_path, original_tags, edited_buckets=None, *,
    reviewed_tags_text=None, tag_scores=None, thresholds=None, app_version="", model_id="",
):
    """Build a review record without changing user-entered tags.

    The initial list is the postprocessed, displayed output (not raw logits).
    Confidence values refer to those displayed tags before manual corrections.
    """
    original_tags = list(dict.fromkeys(original_tags))
    if reviewed_tags_text is None and edited_buckets is None:
        raise ValueError("Provide reviewed_tags_text or edited_buckets")
    final_tags = (parse_tag_text(reviewed_tags_text) if reviewed_tags_text is not None
                  else parse_edited_tags(edited_buckets))
    if not final_tags:
        raise ValueError("Final tag list is empty")
    original_set = set(original_tags)
    final_set = set(final_tags)
    scores = tag_scores or {}
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "image_sha256": image_sha256(image_path),
        "app_version": str(app_version),
        "model_id": str(model_id),
        "thresholds": {key: float(val) for key, val in (thresholds or {}).items()},
        "initial_tags": original_tags,
        "initial_confidences": {
            tag: float(scores[tag]) for tag in original_tags if tag in scores
        },
        "reviewed_tags": final_tags,
        "added": [tag for tag in final_tags if tag not in original_set],
        "removed": [tag for tag in original_tags if tag not in final_set],
        "review_source": "final_list" if reviewed_tags_text is not None else "buckets",
        "reviewed_buckets": (
            {bucket: str(edited_buckets.get(bucket, "")).strip() for bucket in BUCKETS}
            if reviewed_tags_text is None else None
        ),
    }


def append_feedback(file_path, record):
    """Append one JSON line only after the user explicitly requests a save."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as dest:
        dest.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path
