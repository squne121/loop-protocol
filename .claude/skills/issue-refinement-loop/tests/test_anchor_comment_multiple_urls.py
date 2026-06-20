from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import run_refinement_preflight as wrapper


def test_multiple_anchor_comment_urls_are_blocked_fail_closed():
    urls = [
        "https://github.com/testowner/testrepo/issues/100#issuecomment-1",
        "https://github.com/testowner/testrepo/issues/100#issuecomment-2",
    ]
    with mock.patch.object(wrapper, "_validate_anchor_comment_url", return_value=(True, [])):
        sorted_urls, blockers = wrapper._validate_anchor_comments_batch(
            urls, "testowner/testrepo", 100, fixture_comments=[]
        )
    assert sorted_urls == []
    assert blockers == [wrapper.BLOCKER_ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED]
