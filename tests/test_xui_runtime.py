from types import SimpleNamespace

from services.xui.runtime import build_sub_link


def test_build_sub_link_does_not_require_server_metadata():
    server = SimpleNamespace(
        base_url="https://panel.example.com:54321/path",
        sub_domain=None,
        subscription_port=2096,
    )

    assert build_sub_link(server, "abc123") == "https://panel.example.com:2096/sub/abc123"


def test_build_sub_link_uses_scheme_prefix_from_sub_domain():
    server = SimpleNamespace(
        base_url="https://panel.example.com:54321",
        sub_domain="http://sub.example.com",
        subscription_port=2096,
    )

    assert build_sub_link(server, "abc123") == "http://sub.example.com:2096/sub/abc123"
