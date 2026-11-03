"""Fixed DNS transport for allowlisted package hosts behind synthetic local DNS."""
from __future__ import annotations

from collections.abc import Callable


def fetch_public_package_dns(host: str) -> dict:
    """No system DNS, proxy variables, redirects, credentials or arbitrary URL."""
    import http.client
    import json
    import socket
    import ssl
    if host not in {"pypi.org", "files.pythonhosted.org", "registry.npmjs.org"}:
        raise ValueError("denied")
    connection = http.client.HTTPConnection("dns.google", 443, timeout=8)
    raw = socket.create_connection(("8.8.8.8", 443), timeout=8)
    try:
        connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname="dns.google")
        connection.request("GET", "/resolve?name=" + host + "&type=A",
                           headers={"Host":"dns.google", "Accept":"application/dns-json"})
        response = connection.getresponse()
        value = response.read(65537)
        if response.status != 200 or len(value) > 65536:
            raise OSError("Package DNS unavailable")
        return json.loads(value)
    finally:
        connection.close()
        raw.close()


def resolve_package_addresses(host: str, port: int, allowed: set[str], *, lookup=None, fetch=None) -> list:
    """A DNS fallback changes transport only; every destination stays public."""
    import ipaddress
    import socket
    if host not in allowed or port not in {80, 443}:
        raise ValueError("denied")
    lookup = lookup or socket.getaddrinfo
    candidates = lookup(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    public = [entry for entry in candidates if ipaddress.ip_address(entry[4][0]).is_global]
    if public:
        return public
    synthetic = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))
    if not candidates or any(not any(ipaddress.ip_address(entry[4][0]) in network for network in synthetic)
                             for entry in candidates):
        raise ValueError("denied")
    # Fixed authenticated resolver queried only for these already allowlisted
    # hostnames. The package TLS tunnel still verifies its original hostname.
    value = (fetch or fetch_public_package_dns)(host)
    if (not isinstance(value, dict) or value.get("Status") != 0 or value.get("TC") is not False
            or value.get("Question") != [{"name":host + ".", "type":1}]
            or not isinstance(value.get("Answer"), list) or not 1 <= len(value["Answer"]) <= 32):
        raise OSError("Package DNS unavailable")
    aliases = {host + "."}
    for _ in range(8):
        for entry in value["Answer"]:
            if isinstance(entry, dict) and entry.get("type") == 5 and entry.get("name") in aliases:
                target = entry.get("data")
                if not isinstance(target, str) or not target.endswith(".") or len(target) > 254:
                    raise OSError("Package DNS unavailable")
                aliases.add(target)
    addresses = []
    for entry in value["Answer"]:
        if not isinstance(entry, dict):
            raise OSError("Package DNS unavailable")
        if entry.get("type") != 1:
            continue
        if entry.get("name") not in aliases:
            raise OSError("Package DNS unavailable")
        address = ipaddress.ip_address(entry.get("data", ""))
        if address.version != 4 or not address.is_global:
            raise ValueError("denied")
        addresses.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(address), port)))
    if not addresses:
        raise OSError("Package DNS unavailable")
    return addresses


def create_cached_package_resolver(allowed: set[str], *, resolver: Callable | None = None,
                                   clock: Callable[[], float] | None = None) -> Callable[[str, int], list]:
    """Reuse only validated public endpoints for 30s, serializing concurrent DNS.

    The proxy-local cache holds at most six host/port entries. Expired, denied
    and failed resolutions never supply a destination. TLS still authenticates
    the original allowlisted package host on every connection.
    """
    import ipaddress
    import socket
    import threading
    import time

    hosts = frozenset(allowed)
    if not hosts or not hosts <= {"pypi.org", "files.pythonhosted.org", "registry.npmjs.org"}:
        raise ValueError("denied")
    lookup = resolver or resolve_package_addresses
    monotonic = clock or time.monotonic
    cache: dict = {}
    lock = threading.Lock()

    def validated(values: list, port: int) -> tuple:
        if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 64:
            raise ValueError("denied")
        result = []
        for entry in values:
            if not isinstance(entry, (list, tuple)) or len(entry) != 5:
                raise ValueError("denied")
            family, socktype, protocol, _, address = entry
            if (family not in {socket.AF_INET, socket.AF_INET6} or socktype != socket.SOCK_STREAM
                    or protocol != socket.IPPROTO_TCP or not isinstance(address, (list, tuple))
                    or len(address) not in {2, 4} or type(address[1]) is not int or address[1] != port):
                raise ValueError("denied")
            ip = ipaddress.ip_address(address[0])
            if (not ip.is_global or (ip.version == 4) != (family == socket.AF_INET)
                    or len(address) != (2 if family == socket.AF_INET else 4)
                    or any(type(n) is not int or n < 0 for n in address[2:])):
                raise ValueError("denied")
            result.append((family, socktype, protocol, "", tuple(address)))
        return tuple(result)

    def resolve(host: str, port: int) -> list:
        if host not in hosts or type(port) is not int or port not in {80, 443}:
            raise ValueError("denied")
        key = host, port
        with lock:
            saved = cache.get(key)
            if saved is not None and monotonic() < saved[0]:
                return list(validated(saved[1], port))
            cache.pop(key, None)
            values = validated(lookup(host, port, set(hosts)), port)
            cache[key] = (monotonic() + 30.0, values)
            return list(values)

    return resolve
