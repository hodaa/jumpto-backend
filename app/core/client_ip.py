"""Working out which address a request really came from.

The login limiter keys on the client address, so getting this wrong decides
whether the lockout holds. Two mistakes are easy here and both are serious:

- Trusting ``X-Forwarded-For`` unconditionally lets anyone who can reach the
  app write their own key, so a guessed password costs them nothing.
- Trusting nothing behind a proxy merges every user into the proxy's address,
  which locks out whole offices and mobile carriers at once.

The rule is therefore: believe the header only when the connection itself came
from a proxy that was configured to be believed. That is the default-deny
direction, so an unconfigured deployment is the safe one.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address

from app.core.logging import get_logger

logger = get_logger(__name__)

AnyAddress = IPv4Address | IPv6Address
AnyNetwork = IPv4Network | IPv6Network
FORWARDED_HEADER = "X-Forwarded-For"


def _in_networks(address: str, networks: list[AnyNetwork]) -> bool:
    """Whether an address falls inside any of the configured networks."""
    try:
        parsed = ip_address(address)
    except ValueError:
        # Not an address at all, so it cannot be a proxy we configured.
        return False
    return any(parsed in network for network in networks)


def _normalize(address: str | None) -> str | None:
    """Return an address in canonical form, or None if it is not one.

    Normalizing does two jobs. It keeps a crafted header out of a varchar(45)
    column, which would otherwise turn a hostile request into a 500. And it
    makes equivalent spellings of one address share a limiter key - the
    expanded and compressed forms of an IPv6 address are the same host and
    should not each get their own budget.
    """
    if not address:
        return None
    try:
        return str(ip_address(address))
    except ValueError:
        return None


def resolve_client_ip(
    *,
    peer: str | None,
    forwarded_for: str | None,
    trusted_networks: list[AnyNetwork],
) -> str | None:
    """Return the address to key a per-client limit on, or None.

    When the peer is not a trusted proxy the forwarded header is ignored
    entirely. When it is, the chain is walked from the right - the entry the
    closest proxy added is the most trustworthy - and the first address that is
    not itself a configured proxy is returned. That is the last address a
    trusted hop observed, and the closest thing to the real client.

    Anything that does not parse as an address is discarded rather than
    truncated: a half-address key would be shared by unrelated clients.
    """
    peer_address = _normalize(peer)

    if not trusted_networks:
        # Nothing is configured, so nothing may assert who the client is.
        return peer_address

    if peer_address is None:
        logger.info("Request had no usable peer address; ignoring the forwarded header")
        return None

    if not _in_networks(peer_address, trusted_networks):
        # A direct connection. Any header on it is the client's own invention.
        return peer_address

    if not forwarded_for:
        return peer_address

    chain = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    if not chain:
        return peer_address

    for candidate in reversed(chain):
        if not _in_networks(candidate, trusted_networks):
            return _normalize(candidate) or peer_address

    # Every hop is a trusted proxy, so the leftmost entry is the only claim
    # about the origin and there is nothing left to corroborate it.
    return _normalize(chain[0]) or peer_address
