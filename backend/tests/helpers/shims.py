"""Transitional import shim: `app.main` imports the reviews router, which imports
`app.agent.runner` (another workstream). While that module is absent, register a stub so the
identity/RBAC suites can import the app. It is a no-op once the real module exists."""

import importlib.util
import sys
import types


def ensure_app_importable() -> None:
    name = "app.agent.runner"
    if name in sys.modules or importlib.util.find_spec(name) is not None:
        return
    mod = types.ModuleType(name)

    async def run_review(*a, **k):  # pragma: no cover
        raise RuntimeError("app.agent.runner stub")

    mod.run_review = run_review
    sys.modules[name] = mod


ensure_app_importable()
