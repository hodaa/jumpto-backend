"""Which address a request is keyed on.

The login limiter keys on the client address, so these cases are the ones that
decide whether the lockout can be walked around.
"""

from __future__ import annotations

from ipaddress import ip_network

import pytest

from app.core.client_ip import resolve_client_ip

# A deployment fronted by one proxy on a private network.
ONE_PROXY = [ip_network("10.0.0.0/8")]
# A deployment fronted by a proxy chain.
TWO_HOPS = [ip_network("10.0.0.0/8"), ip_network("172.16.0.0/12")]


def resolve(peer, forwarded, networks):  # noqa: ANN001, ANN201
    return resolve_client_ip(peer=peer, forwarded_for=forwarded, trusted_networks=networks)


class TestUnconfiguredDeployment:
    """Default is to trust nothing, which is the safe direction to fail."""

    def test_the_forwarded_header_is_ignored(self) -> None:
        assert resolve("203.0.113.5", "1.2.3.4", []) == "203.0.113.5"

    def test_a_crafted_header_cannot_choose_the_key(self) -> None:
        # The attack the old code allowed: every attempt claims a new address.
        keys = {resolve("203.0.113.5", f"1.2.3.{n}", []) for n in range(20)}
        assert keys == {"203.0.113.5"}

    def test_a_missing_peer_yields_nothing_to_key_on(self) -> None:
        assert resolve(None, "1.2.3.4", []) is None


class TestDirectConnection:
    """Even with proxies configured, a client that is not behind one is believed."""

    def test_a_direct_client_cannot_spoof_a_forwarded_address(self) -> None:
        assert resolve("203.0.113.5", "1.2.3.4", ONE_PROXY) == "203.0.113.5"

    def test_spoofing_cannot_produce_distinct_keys(self) -> None:
        keys = {resolve("203.0.113.5", f"1.2.3.{n}", ONE_PROXY) for n in range(20)}
        assert keys == {"203.0.113.5"}


class TestBehindATrustedProxy:
    def test_the_real_client_is_recovered(self) -> None:
        assert resolve("10.0.0.1", "203.0.113.9", ONE_PROXY) == "203.0.113.9"

    def test_a_chain_is_walked_from_the_trusted_side(self) -> None:
        # The leftmost entry is the client's own claim and the least trusted.
        assert resolve("10.0.0.1", "1.2.3.4, 203.0.113.9, 10.0.0.2", TWO_HOPS) == "203.0.113.9"

    def test_a_client_appending_fake_hops_cannot_hide_itself(self) -> None:
        # The attacker controls the right-hand entries it appends to its own
        # request; only the leftmost survives because the proxy overwrites it.
        # Here a fake hop sits between the real client and the proxy.
        assert resolve("10.0.0.1", "203.0.113.9, 1.2.3.4", ONE_PROXY) == "1.2.3.4"

    def test_spaces_and_empty_entries_are_tolerated(self) -> None:
        assert resolve("10.0.0.1", "  203.0.113.9 ,, ", ONE_PROXY) == "203.0.113.9"

    def test_an_empty_header_falls_back_to_the_proxy(self) -> None:
        assert resolve("10.0.0.1", "", ONE_PROXY) == "10.0.0.1"

    def test_a_chain_of_only_proxies_uses_the_leftmost_claim(self) -> None:
        # Nothing left to corroborate it, but refusing outright would merge
        # real users, so the single remaining claim is used.
        assert resolve("10.0.0.1", "1.2.3.4, 10.0.0.2", ONE_PROXY) == "1.2.3.4"

    def test_a_proxy_on_a_different_range_is_not_trusted(self) -> None:
        assert resolve("172.32.0.1", "1.2.3.4", ONE_PROXY) == "172.32.0.1"


class TestJunkInput:
    """A hostile header must not become a key, and must not error."""

    def test_an_unparseable_header_falls_back_to_the_peer(self) -> None:
        assert resolve("10.0.0.1", "not-an-ip", ONE_PROXY) == "10.0.0.1"

    def test_an_unparseable_peer_is_refused(self) -> None:
        assert resolve("nonsense", "1.2.3.4", ONE_PROXY) is None

    def test_a_hostname_is_not_an_address(self) -> None:
        # A proxy speaking a hostname is not something to key a limiter on.
        assert resolve("10.0.0.1", "evil.example.com", ONE_PROXY) == "10.0.0.1"

    def test_a_very_long_header_cannot_overflow_the_column(self) -> None:
        address = resolve("10.0.0.1", "x" * 5000, ONE_PROXY)
        assert address == "10.0.0.1"
        assert len(address) <= 45

    def test_an_oversized_value_mixed_with_a_real_address_is_discarded(self) -> None:
        assert resolve("10.0.0.1", "y" * 5000, ONE_PROXY) == "10.0.0.1"


class TestNormalization:
    def test_ipv6_is_stored_in_one_canonical_form(self) -> None:
        # The same host written three ways must not get three limiter budgets.
        forms = {
            resolve("10.0.0.1", "2001:0DB8:0000:0000:0000:0000:0000:0001", ONE_PROXY),
            resolve("10.0.0.1", "2001:db8::1", ONE_PROXY),
            resolve("10.0.0.1", "2001:db8:0:0:0:0:0:1", ONE_PROXY),
        }
        assert forms == {"2001:db8::1"}

    def test_every_result_fits_the_column(self) -> None:
        # The widest address there is, expanded, must not overflow varchar(45).
        assert len(resolve("10.0.0.1", "2001:0db8:85a3:0000:0000:8a2e:0370:7334", ONE_PROXY)) <= 45

    def test_ipv4_mapped_ipv6_normalizes(self) -> None:
        assert resolve("10.0.0.1", "::ffff:203.0.113.9", ONE_PROXY) == "::ffff:203.0.113.9"


class TestIpv6Peers:
    def test_an_ipv6_proxy_is_trusted_when_configured(self) -> None:
        proxies = [ip_network("fd00::/8")]
        assert resolve("fd00::1", "203.0.113.9", proxies) == "203.0.113.9"

    def test_an_ipv6_client_cannot_spoof_a_forwarded_address(self) -> None:
        proxies = [ip_network("fd00::/8")]
        assert resolve("2001:db8::1", "1.2.3.4", proxies) == "2001:db8::1"


@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        ("203.0.113.5", None, "203.0.113.5"),
        ("203.0.113.5", "", "203.0.113.5"),
        ("203.0.113.5", "   ", "203.0.113.5"),
    ],
)
def test_absent_headers_are_harmless(peer, forwarded, expected) -> None:  # noqa: ANN001
    assert resolve(peer, forwarded, ONE_PROXY) == expected
