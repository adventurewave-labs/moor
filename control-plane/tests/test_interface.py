"""Interface conformance: the test double must mirror the real gateway.

FakeGateway is the contract: every public method of the REAL
DockerGateway must exist on the fake with the same name. The fake may
add scenario helpers (seed/kill/mutate_env/...), but it may never miss
a real method — otherwise tests pass while production raises
AttributeError. Introduced after exactly that happened: the chaos
endpoint called `kill_container`, the fake had it, the real gateway
had `kill` — and only a live run caught it.
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_fake_gateway_implements_every_real_gateway_method():
    from moor.docker_client import DockerGateway
    from tests.conftest import FakeGateway

    def public_methods(cls) -> set[str]:
        return {
            name
            for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
            if not name.startswith("_")
        }

    real = public_methods(DockerGateway)
    fake = public_methods(FakeGateway)
    missing = real - fake
    assert not missing, (
        f"FakeGateway is missing real DockerGateway methods: {sorted(missing)} "
        "(tests would pass while production raises AttributeError)"
    )
