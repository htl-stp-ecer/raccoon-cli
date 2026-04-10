"""IDE backend module for Raccoon Web IDE."""

from raccoon_cli.ide.launcher import PyCharmLauncher

__all__ = ["PyCharmLauncher", "create_app"]


def create_app(*args, **kwargs):
    """Create the FastAPI application for the IDE backend."""
    from raccoon_cli.ide.app import create_app as _create_app
    return _create_app(*args, **kwargs)
