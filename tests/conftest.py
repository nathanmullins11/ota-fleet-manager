"""Shared test setup.

The services are separate Docker images, each importing its siblings by bare name
(`from config import ...`), and both the orchestrator and the device agent have a
`config.py`. To test them in one pytest session without restructuring the services,
each module is loaded from its file under a unique name (orch_messages, agent_config, ...).
While a module runs, a bare import of a sibling (`from config import ...`) resolves to that
service's own copy, loaded once under its unique name.
"""
import importlib.abc
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


class _SiblingImports(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolves `import <sibling>` to load_service_module(service, sibling)."""

    def __init__(self, service: str):
        self.service = service

    def find_spec(self, name, path=None, target=None):
        if path is None and (SERVICES[self.service] / f"{name}.py").is_file():
            return importlib.util.spec_from_loader(name, self)
        return None

    def create_module(self, spec):
        return load_service_module(self.service, spec.name)

    def exec_module(self, module):
        pass                                    # already executed by load_service_module


def load_service_module(service: str, module: str):
    """Import deploy/<service>/<module>.py as '<service>_<module>'."""
    name = f"{service}_{module}"
    if name in sys.modules:
        return sys.modules[name]
    folder = SERVICES[service]
    path = folder / f"{module}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    siblings = {p.stem for p in folder.glob("*.py")}
    saved = {n: sys.modules.pop(n) for n in siblings if n in sys.modules}
    finder = _SiblingImports(service)
    sys.meta_path.insert(0, finder)
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        del sys.modules[name]
        raise
    finally:
        sys.meta_path.remove(finder)
        for n in siblings:                      # bare names must not leak to other services
            sys.modules.pop(n, None)
        sys.modules.update(saved)
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


@pytest.fixture(scope="session")
def agent_commands():
    return load_service_module("agent", "commands")


@pytest.fixture(scope="session")
def installer():
    return load_service_module("agent", "installer")


@pytest.fixture(scope="session")
def firmware_runner():
    return load_service_module("agent", "firmware_runner")


@pytest.fixture(scope="session")
def agent():
    return load_service_module("agent", "agent")


# --------------------------------------------------------------------------- signed test packages

@pytest.fixture(scope="session")
def signing_key(release):
    """A throwaway release key, so tests never need the real private key."""
    return release.Ed25519PrivateKey.generate()


@pytest.fixture(scope="session")
def packages(release, signing_key, tmp_path_factory):
    """firmware/1.0, 2.0 and 2.1 built and signed with signing_key, plus 9.0, which exits at
    once with code 3 (firmware that does not start). Returns the packages folder."""
    out = tmp_path_factory.mktemp("packages")
    for version in ("1.0", "2.0", "2.1"):
        release.build(version, release.FIRMWARE_DIR / version, out, signing_key)
    broken = tmp_path_factory.mktemp("src") / "9.0"
    broken.mkdir()
    (broken / "main.py").write_text('VERSION = "9.0"\nimport sys\nsys.exit(3)\n')
    release.build("9.0", broken, out, signing_key)
    return out
