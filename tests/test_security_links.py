from china_a_share.security_links import (
    is_security_code_column,
    security_quote_page_url,
)


def test_a_share_codes_link_to_plain_quote_pages():
    assert (
        security_quote_page_url("688220.SH")
        == "https://stockpage.10jqka.com.cn/688220/"
    )
    assert (
        security_quote_page_url("000002.sz")
        == "https://stockpage.10jqka.com.cn/000002/"
    )
    assert (
        security_quote_page_url("830799.BJ")
        == "https://stockpage.10jqka.com.cn/830799/"
    )
    assert (
        security_quote_page_url("600519")
        == "https://stockpage.10jqka.com.cn/600519/"
    )


def test_hk_codes_link_with_hk_prefix_pages():
    assert (
        security_quote_page_url("HK0002")
        == "https://stockpage.10jqka.com.cn/HK0002/"
    )
    assert (
        security_quote_page_url("00700.HK")
        == "https://stockpage.10jqka.com.cn/HK0700/"
    )
    assert (
        security_quote_page_url("2.HK")
        == "https://stockpage.10jqka.com.cn/HK0002/"
    )


def test_unmappable_identifiers_have_no_link():
    assert security_quote_page_url("AAPL") is None
    assert security_quote_page_url(None) is None
    assert security_quote_page_url("") is None
    assert security_quote_page_url("not a code") is None
    assert security_quote_page_url("20260918") is None


def test_column_qualifies_for_links_only_for_security_identifiers():
    suffixed = ["688220.SH", "000002.SZ"]
    assert is_security_code_column("ts_code", suffixed) is True
    assert is_security_code_column("signal", suffixed) is True
    assert is_security_code_column("hk_code", ["00700.HK", "2.HK"]) is True

    # Bare six-digit values under an arbitrary column name stay plain so date
    # or metric columns are never mistaken for security codes.
    bare = ["600519", "000002"]
    assert is_security_code_column("value", bare) is False
    assert is_security_code_column("code", bare) is True

    assert is_security_code_column("signal_date", ["202604", "202605"]) is False

    assert is_security_code_column("symbol", ["AAPL", "600519.SH"]) is True
    assert is_security_code_column("symbol", ["AAPL", "MSFT"]) is False
