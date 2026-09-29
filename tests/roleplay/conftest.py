"""Role unit tests never launch gateways, so they need no real listening ports."""

from itertools import count

import pytest


@pytest.fixture(autouse=True)
def _isolated_ports(monkeypatch):
    ports = count(20000)
    monkeypatch.setattr("nanobot.roleplay.manager.free_port", lambda: next(ports))
