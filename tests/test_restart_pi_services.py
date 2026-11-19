from rich.console import Console

from raccoon_cli.commands import update


class _Channel:
    def recv_exit_status(self):
        return 0


class _Stream:
    channel = _Channel()

    def read(self):
        return b""


class FakeSsh:
    def __init__(self):
        self.commands = []

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        return None, _Stream(), _Stream()


def test_restart_covers_the_camera_service_after_the_bundle_file():
    ssh = FakeSsh()
    update._restart_pi_services(Console(quiet=True), ssh)

    cam = [c for c in ssh.commands if "raccoon-cam.service" in c]
    assert len(cam) == 1
    # Only when the unit exists: a Pi without raccoon-cam must not report a failed restart.
    assert cam[0].startswith("if systemctl cat raccoon-cam.service")
    # The server restarts last, without waiting, because it is the one we talk to.
    assert "raccoon.service" in ssh.commands[-1] and "--no-block" in ssh.commands[-1]
    assert ssh.commands.index(cam[0]) < len(ssh.commands) - 1


def test_the_camera_service_is_not_in_the_list_restarted_in_one_call():
    # Restarted in one `systemctl restart a b c`, a missing unit would mark the whole call failed.
    assert update._CAM_SERVICE not in update._PI_SERVICES
