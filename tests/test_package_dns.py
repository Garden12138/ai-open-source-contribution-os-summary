from __future__ import annotations
import socket
import pytest
from app.sandbox_worker.package_dns import resolve_package_addresses, fetch_public_package_dns

ALLOWED = {"pypi.org", "files.pythonhosted.org"}


def addresses(*values):
    return [(socket.AF_INET6 if ":" in value else socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (value,443))
            for value in values]


def answer(host, ips):
    return {"Status":0, "TC":False, "Question":[{"name":host+".","type":1}],
            "Answer":[{"name":host+".", "type":1, "data":ip} for ip in ips]}


def test_synthetic_dns_uses_verified_public_answers_only():
    calls=[]
    def fetch(host):
        calls.append(host)
        return answer(host,["151.101.0.223"])
    result=resolve_package_addresses("pypi.org",443,ALLOWED,
        lookup=lambda *a,**kw:addresses("198.18.0.5","2001:2::7"),fetch=fetch)
    assert result==addresses("151.101.0.223") and calls==["pypi.org"]


@pytest.mark.parametrize("values",[("127.0.0.1",),("10.0.0.1",),("169.254.169.254",),("::1",),
                                 ("198.18.0.5","10.0.0.1")])
def test_non_synthetic_private_answers_stay_denied(values):
    with pytest.raises(ValueError,match="denied"):
        resolve_package_addresses("pypi.org",443,ALLOWED,lookup=lambda *a,**kw:addresses(*values),
            fetch=lambda *a:pytest.fail("No fallback for private destinations"))


@pytest.mark.parametrize("value",[answer("evil.example",["151.101.0.223"]),
    answer("pypi.org",["127.0.0.1"]),answer("pypi.org",["198.18.0.5"]),
    answer("pypi.org",["151.101.0.223","10.0.0.1"]),answer("pypi.org",[]),
    {**answer("pypi.org",["151.101.0.223"]),"TC":True}])
def test_fallback_rejects_wrong_question_private_mixed_and_incomplete_evidence(value):
    with pytest.raises((OSError,ValueError)):
        resolve_package_addresses("pypi.org",443,ALLOWED,
            lookup=lambda *a,**kw:addresses("198.18.0.5"),fetch=lambda *a:value)


def test_public_system_dns_uses_no_external_fallback():
    assert resolve_package_addresses("pypi.org",443,ALLOWED,
        lookup=lambda *a,**kw:addresses("151.101.0.223","2001:2::7"),
        fetch=lambda *a:pytest.fail("No fallback needed"))==addresses("151.101.0.223")


@pytest.mark.parametrize("host,port",[("169.254.169.254",443),("evil.example",443),("pypi.org",8080)])
def test_rejects_unapproved_host_and_port_before_dns(host,port):
    with pytest.raises(ValueError):
        resolve_package_addresses(host,port,ALLOWED,lookup=lambda *a,**kw:pytest.fail("No DNS"))


def test_fixed_resolver_cannot_fetch_arbitrary_hosts():
    with pytest.raises(ValueError):
        fetch_public_package_dns("evil.example/path?token=canary")


def test_cname_answer_is_bound_to_the_original_question():
    value=answer("files.pythonhosted.org",[])
    value["Answer"]=[{"name":"files.pythonhosted.org.","type":5,"data":"python.map.fastly.net."},
                     {"name":"python.map.fastly.net.","type":1,"data":"151.101.0.223"}]
    lookup=lambda *a,**kw:addresses("198.18.0.6")
    assert resolve_package_addresses("files.pythonhosted.org",443,ALLOWED,lookup=lookup,fetch=lambda *a:value)
    value["Answer"][1]["name"]="unrelated.example."
    with pytest.raises(OSError):
        resolve_package_addresses("files.pythonhosted.org",443,ALLOWED,lookup=lookup,fetch=lambda *a:value)
