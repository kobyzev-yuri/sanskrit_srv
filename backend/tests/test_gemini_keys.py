"""Google AI Studio key pool: parse labels, skip daily-exhausted keys."""
from types import SimpleNamespace

from app.services.gemini_keys import (
    configured_studio_entries,
    is_key_exhausted,
    mark_daily_exhausted,
    parse_studio_key_file,
    pick_studio_key,
    pool_summary,
    studio_keys_to_try,
)


def test_parse_labeled_file_and_env():
    raw = "\n".join(
        [
            "",
            "AQ." + "A" * 40,
            "sanskrit_srv_1",
            "AQ." + "B" * 40,
            "sanskrit_srv_2",
            "AQ." + "C" * 40,
            "",
        ]
    )
    parsed = parse_studio_key_file(raw)
    assert [label for label, _ in parsed] == ["default", "sanskrit_srv_1", "sanskrit_srv_2"]
    assert all(key.startswith("AQ.") for _, key in parsed)

    env = "sanskrit_srv_1:AQ." + "B" * 40 + ",sanskrit_srv_2:AQ." + "C" * 40
    from app.services.gemini_keys import parse_studio_key_blob

    env_parsed = parse_studio_key_blob(env)
    assert env_parsed[0][0] == "sanskrit_srv_1"
    assert env_parsed[1][0] == "sanskrit_srv_2"


def test_pool_skips_exhausted_and_falls_back(tmp_path):
    k1 = "AQ." + "a" * 40
    k2 = "AQ." + "b" * 40
    storage = tmp_path / "storage"
    storage.mkdir()
    settings = SimpleNamespace(
        storage_root=storage,
        gemini_api_key=k1,
        gemini_api_keys=f"sanskrit_srv_1:{k2}",
        gemini_keys_file="",
    )
    labels = [label for label, _ in configured_studio_entries(settings)]
    assert labels == ["default", "sanskrit_srv_1"]
    assert studio_keys_to_try(k1, settings=settings) == [k1, k2]
    mark_daily_exhausted(k1, settings=settings)
    assert is_key_exhausted(k1, settings=settings)
    assert studio_keys_to_try(k1, settings=settings) == [k2]
    assert pick_studio_key(settings) == k2
    summ = pool_summary(settings)
    assert summ["n"] == 2
    assert summ["available"] == 1
    assert summ["available_labels"] == ["sanskrit_srv_1"]


def test_explicit_key_outside_pool_is_not_rotated(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    settings = SimpleNamespace(
        storage_root=storage,
        gemini_api_key="AQ." + "a" * 40,
        gemini_api_keys="sanskrit_srv_1:AQ." + "b" * 40,
        gemini_keys_file="",
    )
    assert studio_keys_to_try("k", settings=settings) == ["k"]
