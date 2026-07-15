import pytest

from imprint.capture.detector import detect_explicit_feedback


@pytest.mark.parametrize("text, marker, route", [
    ("No, use the compact synthetic card.", "direct", "correction"),
    ("Why did you remove the neutral heading?", "question_form", "correction"),
    ("This is not landing; it feels too broad.", "indirect", "correction"),
    ("I prefer the neutral version over the ornate one.", "preference", "preference"),
    ("We must keep source references on every claim.", "standard", "standard"),
    ("Approved. Ship it.", "approval", "approval"),
    ("I reject this synthetic draft.", "rejection", "refusal"),
    ("Do not publish that synthetic example.", "refusal", "refusal"),
])
def test_explicit_feedback_forms(text, marker, route):
    result = detect_explicit_feedback(text, prior_assistant_output="synthetic output")
    assert result.is_feedback and result.marker == marker and result.route == route


def test_polite_feedback_requires_prior_output():
    text = "Please keep the second heading and remove the first."
    assert detect_explicit_feedback(text).is_feedback is False
    assert detect_explicit_feedback(text, prior_assistant_output="a draft").marker == "polite"


def test_silent_reask_is_an_operator_reask_not_silence():
    result = detect_explicit_feedback(
        "Create a concise neutral summary with source labels.",
        prior_operator_text="Create a concise neutral summary with source labels",
        prior_assistant_output="an unrelated output",
    )
    assert result.is_feedback and result.marker == "silent_reask"
    assert detect_explicit_feedback("", prior_assistant_output="anything").is_feedback is False


@pytest.mark.parametrize("text", [
    "What time is the synthetic review?", "I don't know the answer.",
    "I have never visited that place.", "Could you create a new summary?",
    "Thanks for the update.", "No idea where the fixture lives.",
])
def test_negative_controls(text):
    assert detect_explicit_feedback(text).is_feedback is False
