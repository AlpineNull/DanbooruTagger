"""Unit tests for opt-in local feedback. No GUI, network, or model needed."""
import json
from pathlib import Path

from feedback_store import append_feedback, build_feedback_record, parse_edited_tags


def test_review_buckets_keep_order_and_remove_duplicates():
    assert parse_edited_tags({
        "high": "1girl solo",
        "medium": "solo blush",
        "low": "  sweatdrop\nblush ",
    }) == ["1girl", "solo", "blush", "sweatdrop"]


def test_feedback_records_are_local_and_keep_edit_history(tmp_path):
    image = tmp_path / "private_image_name.png"
    image.write_bytes(b"example image content")
    record = build_feedback_record(
        image,
        ["1girl", "solo", "sparkle"],
        {"high": "1girl solo", "medium": "fingerless_gloves", "low": ""},
        tag_scores={"1girl": 0.98, "solo": 0.91, "sparkle": 0.18},
        thresholds={"high": 0.8, "medium": 0.5, "low": 0.17},
        app_version="0.7.3",
        model_id="pixai-labs/pixai-tagger-v1.0",
    )
    assert record["added"] == ["fingerless_gloves"]
    assert record["removed"] == ["sparkle"]
    assert record["initial_confidences"]["sparkle"] == 0.18
    assert record["image_sha256"] and len(record["image_sha256"]) == 64
    assert str(image) not in json.dumps(record)
    assert "private_image_name" not in json.dumps(record)
    assert "image_path" not in record
    assert "image_bytes" not in record

    feedback_file = tmp_path / "feedback" / "corrections.jsonl"
    append_feedback(feedback_file, record)
    parsed = json.loads(feedback_file.read_text(encoding="utf-8").splitlines()[0])
    assert parsed["reviewed_tags"] == ["1girl", "solo", "fingerless_gloves"]


def test_feedback_can_record_review_without_additions(tmp_path):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"abc")
    record = build_feedback_record(image, ["1girl"], {
        "high": "1girl", "medium": "", "low": "",
    })
    assert record["added"] == []
    assert record["removed"] == []
    assert record["reviewed_tags"] == ["1girl"]


def test_flat_final_list_does_not_require_original_confidence_buckets(tmp_path):
    image = tmp_path / "new_image.png"
    image.write_bytes(b"image content")
    record = build_feedback_record(
        image, ["1girl", "solo", "sparkle", "upper_body"],
        reviewed_tags_text="1girl solo cropped_torso cleavage cropped_torso",
        tag_scores={"1girl": 0.99, "solo": 0.91, "sparkle": 0.12,
                    "upper_body": 0.47},
    )
    assert record["review_source"] == "final_list"
    assert record["reviewed_tags"] == ["1girl", "solo", "cropped_torso", "cleavage"]
    assert record["added"] == ["cropped_torso", "cleavage"]
    assert record["removed"] == ["sparkle", "upper_body"]
    assert record["reviewed_buckets"] is None


def test_flat_final_list_rejects_empty_input(tmp_path):
    import pytest
    image = tmp_path / "image.png"
    image.write_bytes(b"image bytes")
    with pytest.raises(ValueError, match="empty"):
        build_feedback_record(image, ["1girl"], reviewed_tags_text="  ")
