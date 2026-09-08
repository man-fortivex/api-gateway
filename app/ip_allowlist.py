import ipaddress


def is_ip_allowed(client_ip: str, allowlist: list[str]) -> bool:
    """Empty allowlist means "allow all" (the default, unrestricted case).
    A non-empty allowlist means the client IP must match at least one
    entry (each entry is a CIDR block, e.g. "203.0.113.0/24", or a bare
    IP which is treated as a /32 or /128)."""
    if not allowlist:
        return True

    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        # Can't parse the client's address at all — fail closed rather
        # than silently allowing an unrecognizable source through.
        return False

    for entry in allowlist:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue  # skip malformed entries rather than crash the request
        if addr in network:
            return True

    return False
