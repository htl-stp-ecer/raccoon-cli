"""The reader moved from /home/pi/stm32_data_reader (tarball) to /usr/bin (.deb): find both."""

from raccoon_cli.server.routes import version


def _fake(found):
    def binary_version(path):
        return found.get(path)

    return binary_version


def test_deb_install_wins_over_the_old_location(monkeypatch):
    monkeypatch.setattr(version, "_binary_version", _fake({
        "/usr/bin/stm32_data_reader": "60",
        "/home/pi/stm32_data_reader/stm32_data_reader": "59",
    }))
    assert version._stm32_version() == "60"


def test_old_install_is_still_found(monkeypatch):
    monkeypatch.setattr(version, "_binary_version", _fake({"/home/pi/stm32_data_reader/stm32_data_reader": "59"}))
    assert version._stm32_version() == "59"


def test_not_installed_is_none(monkeypatch):
    monkeypatch.setattr(version, "_binary_version", _fake({}))
    assert version._stm32_version() is None


def test_the_reader_is_updated_as_a_deb():
    from raccoon_cli.version_checker import PACKAGE_REGISTRY

    reader = next(p for p in PACKAGE_REGISTRY if p.name == "stm32-data-reader")
    assert reader.pi_install == "deb"
    assert reader.pi_asset == "stm32-data-reader_*_arm64.deb"
    assert "/usr/bin/stm32_data_reader" in reader.version_cmd
