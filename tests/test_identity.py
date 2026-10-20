import json
from types import SimpleNamespace
from app.identity_redact import IdentityRedactor, StreamRedactor, redact_stream
from app.conversation import build_standard_transcript, build_lite_transcript

ACC = {"user_id": "uid-123", "space_id": "sid-456"}
SECRETS = ["uid-123", "sid-456"]


def test_standard_transcript_has_no_identity():
    t = build_standard_transcript([{"role": "system", "content": "be nice"}, {"role": "user", "content": "hi"},
                                   {"role": "assistant", "content": "yo"}, {"role": "user", "content": "q"}],
                                  "claude-sonnet4.6", ACC)
    blob = json.dumps(t)
    for s in SECRETS + ["userId", "userName", "userEmail", "spaceId"]:
        assert s not in blob
    cfg = t[0]["value"]
    assert cfg["searchScopes"] == [] and cfg["useWebSearch"] is True


def test_lite_transcript_has_no_identity():
    assert "userId" not in json.dumps(build_lite_transcript("hi", "claude-sonnet4.6"))


def test_heavy_blocks_have_no_identity():
    from app.conversation import ConversationManager
    cm = ConversationManager.__new__(ConversationManager)
    client = SimpleNamespace(user_id="uid-123", space_id="sid-456", space_view_id="sv", user_name="ann lee", user_email="a@b.com")
    blob = json.dumps([cm._build_context_block(client), cm._build_context_block(client, gemini_mode=True),
                       cm._build_config_block("claude-sonnet4.6"), cm._build_config_block("x", gemini_mode=True),
                       cm._build_dialog_block("user", "hi", client)])
    for s in SECRETS + ["ann lee", "a@b.com", "sv\"", "everything"]:
        assert s not in blob


R = IdentityRedactor(names=["ann lee"], emails=["jdoe.test@example.com"])


def test_redact_basic():
    assert R.redact("Hi ann lee! Mail jdoe.test@example.com") == "Hi there! Mail [redacted]"
    assert R.redact("Hi Ann Lee.") == "Hi there."
    assert R.redact("creator: <mention-user url=\"user://11111111-2222-3333-4444-555555555555\"/> ok") == "creator:  ok"
    assert R.redact("annlee is fine") == "annlee is fine"


def test_stream_split_terms():
    text = "Hi ann lee! Your email is jdoe.test@example.com. " * 3 + "x" * 400 + " bye ann lee"
    for step in (1, 2, 3, 7, 50):
        sr = StreamRedactor(R)
        out = "".join(sr.feed(text[i:i + step]) for i in range(0, len(text), step)) + sr.flush()
        assert out == R.redact(text)
        assert "ann lee" not in out and "jdoe.test" not in out


def test_redact_stream_items():
    items = [{"type": "content", "text": "Hi an"}, {"type": "search", "data": {}},
             {"type": "content", "text": "n lee!"}, {"type": "usage", "x": 1}]
    out = list(redact_stream(iter(items), R))
    assert "".join(i["text"] for i in out if i["type"] == "content") == "Hi there!"
    assert [i["type"] for i in out if i["type"] != "content"] == ["search", "usage"]
    fin = list(redact_stream(iter([{"type": "final_content", "text": "Hi ann lee", "source_type": "x"}]), R))
    assert fin == [{"type": "final_content", "text": "Hi there", "source_type": "x"}]
    assert list(redact_stream(iter(items), None)) == items
