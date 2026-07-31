import json
from pathlib import Path


def test_anchor_comment_schema_rejects_classification_alias_fields():
    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "schemas" / "anchor_comment.schema.json").read_text(
            encoding="utf-8"
        )
    )
    props = schema["properties"]
    assert "classification" not in props
    assert "fact_check_status" not in props
    for field in [
        "preliminary_classification",
        "final_classification",
        "classification_reason",
        "requires_fact_check",
    ]:
        assert field in props
