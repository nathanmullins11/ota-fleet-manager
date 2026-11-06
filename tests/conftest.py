"""Shared test setup.

The services are separate Docker images, each importing its siblings by bare name
(`from config import ...`), and both the orchestrator and the device agent have a
`config.py`. To test them in one pytest session without restructuring the services,
each module is loaded from its file under a unique name (orch_messages, agent_config, ...).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"

SERVICES = {
    "orch": DEPLOY / "orchestrator",
    "agent": DEPLOY / "device-agent",
    "registry": DEPLOY / "registries" / "software",
    "tools": REPO / "tools",
}


def load_service_module(service: str, module: str):
    """Import deploy/<service>/<module>.py as '<service>_<module>'."""
    name = f"{service}_{module}"
    if name in sys.modules:
        return sys.modules[name]
    path = SERVICES[service] / f"{module}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def messages():
    return load_service_module("orch", "messages")


@pytest.fixture(scope="session")
def topics():
    return load_service_module("orch", "topics")


@pytest.fixture(scope="session")
def agent_config():
    return load_service_module("agent", "config")


@pytest.fixture(scope="session")
def registry():
    return load_service_module("registry", "app")


@pytest.fixture(scope="session")
def release():
    return load_service_module("tools", "release")


@pytest.fixture(scope="session")
def rollout_logic():
    return load_service_module("orch", "rollout_logic")


@pytest.fixture(scope="session")
def orch_registry():
    return load_service_module("orch", "registry")
