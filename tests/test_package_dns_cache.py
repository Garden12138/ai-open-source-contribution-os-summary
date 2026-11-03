"""Bounded public DNS reuse must not admit stale or private destinations."""
from concurrent.futures import ThreadPoolExecutor
import socket
import threading

import pytest

from app.sandbox_worker import package_dns


def factory(*args, **kwargs):
    return package_dns.create_cached_package_resolver(*args, **kwargs)


def public(port=443):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('151.101.0.223', port))]


def test_concurrent_allowed_requests_share_one_resolution_without_sharing_mutable_results():
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = public()

    def resolve(host, port, allowed):
        calls.append((host, port, allowed))
        entered.set()
        assert release.wait(5)
        return original

    cached = factory({'registry.npmjs.org'}, resolver=resolve)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(cached, 'registry.npmjs.org', 443) for _ in range(8)]
        assert entered.wait(5)
        release.set()
        results = [f.result(5) for f in futures]
    assert len(calls) == 1 and results == [public()] * 8
    original[0] = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443))
    results[0].clear()
    assert cached('registry.npmjs.org', 443) == public()


def test_expired_cache_is_not_a_fallback_after_failed_refresh():
    now, calls = [0.0], []

    def resolve(host, port, allowed):
        calls.append(host)
        if len(calls) == 2:
            raise OSError('unavailable')
        return public(port)

    cached = factory({'pypi.org'}, resolver=resolve, clock=lambda: now[0])
    assert cached('pypi.org', 443) == public()
    now[0] = 29.999
    assert cached('pypi.org', 443) == public() and len(calls) == 1
    now[0] = 30.0
    with pytest.raises(OSError):
        cached('pypi.org', 443)
    assert len(calls) == 2
    assert cached('pypi.org', 443) == public() and len(calls) == 3


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '198.18.0.1', '::1'])
def test_invalid_destination_is_never_cached_even_if_other_addresses_are_public(address):
    count = []

    def resolve(*args):
        count.append(1)
        return public() + [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 443))]

    cached = factory({'pypi.org'}, resolver=resolve)
    for _ in range(2):
        with pytest.raises(ValueError):
            cached('pypi.org', 443)
    assert len(count) == 2


def test_mutating_allowlist_cannot_expand_the_frozen_cache_authority():
    allowed = {'pypi.org'}
    calls = []
    cached = factory(allowed, resolver=lambda host, port, hosts: calls.append(host) or public(port))
    assert cached('pypi.org', 443) == public()
    allowed.add('files.pythonhosted.org')
    for host, port in [('files.pythonhosted.org', 443), ('pypi.org', 8080), ('pypi.org', True)]:
        with pytest.raises(ValueError):
            cached(host, port)
    assert calls == ['pypi.org']


def test_ports_and_hosts_have_separate_entries_and_unknown_hosts_cannot_allocate_a_cache():
    calls = []
    cached = factory({'pypi.org', 'files.pythonhosted.org'},
        resolver=lambda host, port, allowed: calls.append((host, port)) or public(port))
    for host, port in [('pypi.org', 80), ('pypi.org', 443), ('files.pythonhosted.org', 443)]:
        assert cached(host, port) == public(port)
        assert cached(host, port) == public(port)
    assert len(calls) == 3
    for allowed in [set(), {'example.com'}, {'pypi.org', 'example.com'}]:
        with pytest.raises(ValueError):
            factory(allowed)


@pytest.mark.parametrize('candidate', [[], public() * 65,
    [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('151.101.0.223', 80))],
    [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, '', ('151.101.0.223', 443))]])
def test_malformed_or_unbounded_resolver_results_fail_closed(candidate):
    cached = factory({'pypi.org'}, resolver=lambda *args: candidate)
    with pytest.raises(ValueError):
        cached('pypi.org', 443)
