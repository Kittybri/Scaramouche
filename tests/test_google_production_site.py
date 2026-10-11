"""Offline assertions for public Google verification preparation."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "deploy" / "connections" / "public"


def test_site_explains_google_scopes_and_links_to_privacy():
    homepage = (PUBLIC / "index.html").read_text()
    privacy = (PUBLIC / "privacy.html").read_text()
    assert 'href="/privacy"' in homepage
    assert "Calendar" in homepage and "Tasks" in homepage
    assert "explicit" in homepage.lower() and "confirmation" in homepage
    for token in ("access and refresh tokens", "Discord user ID", "text-generation provider", "disconnect"):
        assert token in privacy
    assert "www.googleapis.com/auth/drive" not in privacy
    assert "www.googleapis.com/auth/gmail" not in privacy


def test_proxy_separates_public_pages_from_oauth():
    conf = (ROOT / "deploy" / "connections" / "nginx.conf.example").read_text()
    assert "location = / {" in conf and "location = /privacy {" in conf
    assert "proxy_pass http://127.0.0.1:8787;" in conf
    assert "oauth/google/(start/[^/]+|callback)" in conf
    assert "access_log off;" in conf



def test_full_bot_privacy_discloses_optional_sensitive_features_and_google_limited_use():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    policy = (root / "privacy.html").read_text()
    scoped = (PUBLIC / "privacy.html").read_text()
    for term in ("voice audio", "face-recognition", "Google Calendar", "Google Tasks", "Google Workspace", "Limited Use", "!google disconnect"):
        assert term in policy
    assert "Limited Use" in scoped
    assert "AI models" in scoped
    assert "https://developers.google.com/workspace/workspace-api-user-data-developer-policy" in scoped
