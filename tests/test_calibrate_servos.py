"""Servo calibration: jogging the servo over the server API."""

import queue
from unittest.mock import MagicMock, patch

from raccoon_cli.commands import calibrate


def test_queued_jogs_are_merged_and_the_end_is_kept():
    moves = queue.Queue()
    for item in (5.0, 5.0, -0.5, None):
        moves.put(item)

    assert calibrate._next_move(moves) == 9.5
    assert calibrate._next_move(moves) is None


def test_unresolvable_host_is_used_as_is():
    with patch.object(calibrate.socket, "getaddrinfo", side_effect=OSError):
        assert calibrate._resolve_ipv4("pi.invalid", 8421) == "pi.invalid"


def test_session_uses_one_client_and_saves_the_offset(tmp_path):
    keys = iter(["\x1b", "[C", "\x1b", "[C", "\x1b", "[A", "\r"])
    angle = [90.0]

    def _post(url, json=None, **_kwargs):
        response = MagicMock()
        if url.endswith("/move"):
            angle[0] += json["delta_to_move"]
            response.json.return_value = {"current_angle": angle[0]}
        elif url.endswith("/end"):
            response.json.return_value = {"delta": angle[0] - 90.0, "final_angle": angle[0]}
        return response

    client = MagicMock()
    client.post.side_effect = _post
    config = {"definitions": {"arm": {"type": "Servo", "port": 0, "positions": {"up": 90}}}}
    ctx = MagicMock(obj={"console": MagicMock(), "local": False})

    with patch("httpx.Client", return_value=client) as client_class, \
            patch("httpx.get"), \
            patch.object(calibrate, "_servo_server_endpoint", return_value=("pi", 1, "t")), \
            patch.object(calibrate, "_resolve_ipv4", return_value="10.0.0.1"), \
            patch.object(calibrate, "_pick_servo",
                         return_value=("arm", config["definitions"]["arm"])), \
            patch.object(calibrate, "_pick_position", return_value=("up", 90.0)), \
            patch.object(calibrate, "_save_servo_offset") as save, \
            patch.object(calibrate.questionary, "confirm") as again, \
            patch.object(calibrate.sys, "stdin") as stdin, \
            patch("termios.tcgetattr"), patch("termios.tcsetattr"), patch("tty.setraw"):
        again.return_value.ask.return_value = False
        stdin.read.side_effect = lambda _n: next(keys)
        calibrate._calibrate_servos(ctx, tmp_path, config)

    client_class.assert_called_once()
    client.close.assert_called_once()
    assert client.post.call_args_list[0].args[0].startswith("http://10.0.0.1:1/")
    # right, right selects the 5 degree step
    save.assert_called_once_with(tmp_path, "arm", 5.0)
