"""Gemini AI Studio vs ProxyAPI request shape."""
from app.services.gemini_client import gemini_headers, is_google_studio_base


def test_studio_uses_goog_api_key_header():
    headers = gemini_headers("k", "https://generativelanguage.googleapis.com")
    assert headers["x-goog-api-key"] == "k"
    assert "Authorization" not in headers
    assert is_google_studio_base("https://generativelanguage.googleapis.com/v1beta")


def test_proxyapi_uses_bearer():
    headers = gemini_headers("px", "https://api.proxyapi.ru/google")
    assert headers["Authorization"] == "Bearer px"
    assert "x-goog-api-key" not in headers


def test_studio_parts_use_camel_case_inline_data():
    from app.services.gemini_client import _normalize_parts

    parts = _normalize_parts(
        [{"text": "hi"}, {"inline_data": {"mime_type": "image/jpeg", "data": "abc"}}],
        studio=True,
    )
    assert parts[0] == {"text": "hi"}
    assert parts[1]["inlineData"]["mimeType"] == "image/jpeg"
    assert parts[1]["inlineData"]["data"] == "abc"
