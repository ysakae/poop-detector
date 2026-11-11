"""GUI を外から開くときの URL・待ち受け・信頼するプロキシの検証と、許す Host と Origin を確かめる。"""

import pytest

from poop_detector.config import Config, ConfigError
from poop_detector.web_address import (
    Site,
    WebOptions,
    valid_listen_host,
    valid_proxies,
    valid_public_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://toilet.example.com",
        "https://toilet.example.com/",
        "https://Toilet.Example.com:8443",
        "http://localhost:8765",
        "",
    ],
)
def test_public_url_accepts_https_domains_and_local_development(url):
    assert valid_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://toilet.example.com",  # 外からの接続は HTTPS だけ
        "https://192.0.2.10",  # パスキーの RP ID に IP アドレスは使えない
        "https://[2001:db8::1]",
        "https://toilet.example.com/gui",  # パスの下には置けない
        "https://toilet.example.com?x=1",
        "https://user:pass@toilet.example.com",
        "ftp://toilet.example.com",
        "toilet.example.com",
        "https://",
        "https://toilet.example.com:99999",
    ],
)
def test_public_url_rejects_what_passkeys_and_cookies_cannot_use(url):
    assert not valid_public_url(url)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "0.0.0.0", "192.168.1.20", "::"])
def test_listen_host_accepts_ip_addresses(host):
    assert valid_listen_host(host)


@pytest.mark.parametrize("host", ["", "localhost", "example.com", "300.1.1.1"])
def test_listen_host_rejects_names(host):
    assert not valid_listen_host(host)


@pytest.mark.parametrize("value", ["", "127.0.0.1", "127.0.0.1, 10.0.0.0/8", "::1"])
def test_trusted_proxies_accept_addresses_and_networks(value):
    assert valid_proxies(value)


@pytest.mark.parametrize("value", ["*", "proxy.local", "10.0.0.0/33", "127.0.0.1,,"])
def test_trusted_proxies_reject_wildcards_and_names(value):
    assert not valid_proxies(value)


def test_site_allows_local_and_public_hosts_and_origins():
    site = Site(8765, "https://Toilet.Example.com/")
    assert site.hosts == {
        "127.0.0.1:8765",
        "localhost:8765",
        "toilet.example.com",
        "toilet.example.com:443",
    }
    assert site.origins == {
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "https://toilet.example.com",
    }
    assert site.public_base == "https://toilet.example.com/"


def test_site_without_public_url_is_local_only():
    site = Site(8765)
    assert site.hosts == {"127.0.0.1:8765", "localhost:8765"}
    assert site.public_base is None


def test_site_keeps_an_explicit_port():
    site = Site(8765, "https://toilet.example.com:8443")
    assert "toilet.example.com:8443" in site.hosts
    assert "toilet.example.com" not in site.hosts
    assert "https://toilet.example.com:8443" in site.origins


def test_gui_settings_come_from_the_config_file():
    config = Config(
        gui_host="0.0.0.0",
        gui_port=9000,
        gui_public_url="https://toilet.example.com",
        gui_trusted_proxies="10.0.0.2",
    )
    options = WebOptions.from_config(config)
    assert options == WebOptions("0.0.0.0", 9000, "https://toilet.example.com", "10.0.0.2")
    assert WebOptions.from_config(config, port=0).port == 0


def test_gui_defaults_stay_local():
    options = WebOptions.from_config(Config())
    assert options == WebOptions("127.0.0.1", 8765, "", "127.0.0.1")


@pytest.mark.parametrize(
    ("field", "value", "name"),
    [
        ("gui_public_url", "http://toilet.example.com", "gui.public_url"),
        ("gui_host", "example.com", "gui.host"),
        ("gui_port", 0, "gui.port"),
        ("gui_trusted_proxies", "*", "gui.trusted_proxies"),
    ],
)
def test_invalid_gui_settings_name_the_item(field, value, name):
    with pytest.raises(ConfigError, match=name):
        Config(**{field: value})
