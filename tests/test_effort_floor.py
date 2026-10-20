import os
import types

import pytest

from app import model_registry as mr

OPUS = "albuquerque-quinn"


@pytest.fixture(autouse=True)
def _floor_env(monkeypatch):
    monkeypatch.delenv("NOTION_EFFORT_FLOOR", raising=False)
    yield


def test_unset_is_noop():
    for e in (None, "none", "low", "medium", "high", "xhigh", "max"):
        assert mr.apply_effort_floor(OPUS, e) == e


@pytest.mark.parametrize("req,exp", [
    (None, "high"), ("low", "high"), ("medium", "high"), ("high", "high"),
    ("xhigh", "xhigh"), ("max", "max"),
])
def test_floor_raises_never_lowers(monkeypatch, req, exp):
    monkeypatch.setenv("NOTION_EFFORT_FLOOR", "albuquerque-quinn:high")
    assert mr.apply_effort_floor(OPUS, req) == exp


def test_public_name_and_other_models(monkeypatch):
    monkeypatch.setenv("NOTION_EFFORT_FLOOR", "claude-opus5.5:high, bogus, x:notalevel")
    assert mr.apply_effort_floor(OPUS, "medium") == "high"
    assert mr.apply_effort_floor("achira-donut", "medium") == "medium"  # sonnet5.5 untouched


def _fake_client():
    from app.notion_client import NotionOpusAPI
    fake = types.SimpleNamespace(_gpt_answers=lambda t: False, _instructions_page_id=lambda: None)
    return lambda tr: NotionOpusAPI._to_notion_transcript(fake, tr)


def _cfg(model, eff):
    v = {"type": "workflow", "model": model}
    if eff:
        v["reasoningEffort"] = eff
    return [{"id": "1", "type": "config", "value": v}, {"id": "2", "type": "user", "value": [["hi"]]}]


@pytest.mark.parametrize("model,req,exp_unset,exp_floor", [
    ("claude-opus5.5", "medium", "medium", "high"),
    ("claude-opus5.5", None, "medium", "high"),
    ("claude-opus5.5", "none", "low", "high"),
    ("claude-opus5.5", "xhigh", "xhigh", "xhigh"),
    ("claude-sonnet5.5", "medium", "medium", "medium"),
])
def test_to_notion_transcript(monkeypatch, model, req, exp_unset, exp_floor):
    conv = _fake_client()
    assert conv(_cfg(model, req))[0]["value"]["reasoningEffort"] == exp_unset
    monkeypatch.setenv("NOTION_EFFORT_FLOOR", "albuquerque-quinn:high")
    assert conv(_cfg(model, req))[0]["value"]["reasoningEffort"] == exp_floor
