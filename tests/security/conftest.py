"""Makes the STRIDE fixtures (tests/security/stride.py) available to the security tests.

tests/security is a package, so this file imports as security.conftest and never shadows
tests/conftest.py for modules that do `from conftest import ...`.
"""
from .stride import (  # noqa: F401
    attacker, command, device, make_package, orch_main, read_repo, registry)
