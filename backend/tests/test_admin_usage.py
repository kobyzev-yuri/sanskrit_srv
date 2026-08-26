"""Admin usage rows are split by network."""
from types import SimpleNamespace
from uuid import uuid4

from app.services.llm_usage import all_projects_usage_summary


class _Scalar:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, projects, events):
        self._projects = projects
        self._events = events

    def scalars(self, _stmt):
        # first call projects, then events — order follows all_projects_usage_summary
        if not getattr(self, "_n", 0):
            self._n = 1
            return _Scalar(self._projects)
        return _Scalar(self._events)


def test_usage_split_by_network():
    pid = uuid4()
    projects = [
        SimpleNamespace(id=pid, slug="book-ru", title="Book", settings={"task": "translate"}),
    ]
    events = [
        SimpleNamespace(
            project_id=pid, network="gemini", prompt_tokens=10, completion_tokens=20, total_tokens=30, ok=True
        ),
        SimpleNamespace(
            project_id=pid, network="openrouter", prompt_tokens=100, completion_tokens=5, total_tokens=105, ok=True
        ),
    ]
    out = all_projects_usage_summary(_Session(projects, events))
    nets = {n["network"]: n for n in out["projects"][0]["by_network"]}
    assert nets["gemini"]["prompt_tokens"] == 10
    assert nets["gemini"]["completion_tokens"] == 20
    assert nets["openrouter"]["prompt_tokens"] == 100
    assert {n["network"] for n in out["by_network"]} == {"gemini", "openrouter"}
    assert out["totals"]["calls"] == 2
    assert out["by_user"]
    assert out["by_user"][0]["key_source"] == "default"
    assert out["by_user"][0]["calls"] == 2


def test_usage_split_by_user_keys():
    pid = uuid4()
    uid = uuid4()
    projects = [
        SimpleNamespace(id=pid, slug="book-ru", title="Book", settings={"task": "translate"}),
    ]
    events = [
        SimpleNamespace(
            project_id=pid,
            network="openrouter",
            prompt_tokens=10,
            completion_tokens=2,
            total_tokens=12,
            ok=True,
            user_id=None,
            key_source="default",
            key_hint="dmin",
        ),
        SimpleNamespace(
            project_id=pid,
            network="gemini",
            prompt_tokens=5,
            completion_tokens=5,
            total_tokens=10,
            ok=True,
            user_id=uid,
            key_source="personal",
            key_hint="9999",
        ),
    ]
    out = all_projects_usage_summary(_Session(projects, events))
    sources = {row["key_source"]: row for row in out["by_user"]}
    assert sources["default"]["total_tokens"] == 12
    assert sources["personal"]["total_tokens"] == 10
    assert sources["personal"]["key_hint"] == "9999"
