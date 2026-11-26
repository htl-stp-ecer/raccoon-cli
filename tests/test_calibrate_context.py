"""Local/remote resolution of the calibrate commands."""

from unittest.mock import MagicMock, patch

import pytest

from raccoon_cli.commands import calibrate


@pytest.mark.parametrize("connects", [True, False])
def test_unreachable_pi_falls_back_to_local(tmp_path, connects):
    manager = MagicMock(is_connected=False)
    manager.load_from_project.return_value = None
    manager.load_known_pis.return_value = [{"address": "pi.test"}]

    def _connect(*_args):
        manager.is_connected = connects
        return connects

    manager.connect_sync.side_effect = _connect
    ctx = MagicMock(obj={"console": MagicMock()})

    with patch.object(calibrate, "require_project", return_value=tmp_path), \
            patch.object(calibrate, "load_project_config", return_value={}), \
            patch("raccoon_cli.client.connection.get_connection_manager", return_value=manager):
        calibrate._prepare_calibration_context(ctx, local=False)

    assert ctx.obj["local"] is not connects


def test_no_known_pi_falls_back_to_local(tmp_path):
    manager = MagicMock(is_connected=False)
    manager.load_from_project.return_value = None
    manager.load_known_pis.return_value = []
    ctx = MagicMock(obj={"console": MagicMock()})

    with patch.object(calibrate, "require_project", return_value=tmp_path), \
            patch.object(calibrate, "load_project_config", return_value={}), \
            patch("raccoon_cli.client.connection.get_connection_manager", return_value=manager):
        calibrate._prepare_calibration_context(ctx, local=False)

    assert ctx.obj["local"] is True


def test_local_servo_calibration_uses_server_on_this_machine():
    config = MagicMock(port=1234, api_token="token")

    with patch("raccoon_cli.server.config.load_config", return_value=config):
        assert calibrate._servo_server_endpoint(local=True) == ("127.0.0.1", 1234, "token")


def test_remote_servo_calibration_uses_connected_pi():
    manager = MagicMock()
    manager.state = MagicMock(pi_address="pi.test", pi_port=8421, api_token="token")

    with patch("raccoon_cli.client.connection.get_connection_manager", return_value=manager):
        assert calibrate._servo_server_endpoint(local=False) == ("pi.test", 8421, "token")
