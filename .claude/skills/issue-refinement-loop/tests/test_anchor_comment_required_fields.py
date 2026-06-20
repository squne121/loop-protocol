import json
from pathlib import Path


def test_anchor_comment_schema_requires_required_metadata_fields():
    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "schemas" / "loop_state.schema.json").read_text(
            encoding="utf-8"
        )
    )
    required = schema["definitions"]["anchor_comment"]["required"]
    for field in [
        "author_association",
        "comment_created_at",
        "comment_updated_at",
        "fetched_at",
        "scope_impact",
        "requires_fact_check",
    ]:
        assert field in required
