<div align="center">

<img src="https://raw.githubusercontent.com/htl-stp-ecer/.github/main/profile/raccoon-logo.svg" alt="raccoon-cli" width="100"/>

# raccoon-cli

**The dev toolchain for RaccoonOS — scaffold, configure, sync, and run Botball robots.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](COPYING)
![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=ffdd54)
[![Build & Release](https://github.com/htl-stp-ecer/raccoon-cli/actions/workflows/build-release.yml/badge.svg)](https://github.com/htl-stp-ecer/raccoon-cli/actions/workflows/build-release.yml)
[![Latest Release](https://img.shields.io/github/v/release/htl-stp-ecer/raccoon-cli)](https://github.com/htl-stp-ecer/raccoon-cli/releases/latest)
![PyPI](https://img.shields.io/pypi/v/raccoon-cli?logo=pypi&logoColor=white)

> 📖 **Full documentation at [raccoon-docs.pages.dev](https://raccoon-docs.pages.dev/)**

</div>

---

`raccoon` is the command-line companion to [raccoon-lib](https://github.com/htl-stp-ecer/raccoon-lib). It handles everything outside the robot code itself: generating hardware boilerplate from a YAML config, syncing files to the Pi, and running your missions remotely.

---

## Installation

```bash
pip install raccoon-cli
```

---

## How it works

raccoon uses a **client-server architecture**. Your laptop runs the CLI; the Pi runs a small FastAPI daemon (`raccoon-server`) that receives commands, manages execution, and streams logs back.

### Pi setup (one time)

```bash
# On the Pi -- install raccoon-server as a systemd service
sudo raccoon-server install
```

After that, the server starts automatically on boot and `raccoon connect` can reach it.

---

## Commands

### Missions

Projects are git repos created with the setup script of [raccoon-example](https://github.com/htl-stp-ecer/raccoon-example) (see [Getting Started](https://raccoon-docs.pages.dev/00-getting-started/03-create-your-project/)); run `raccoon` from inside the project.

| Command | Description |
|:--------|:------------|
| `raccoon missions create <[Mxxx]name>...` | Add one or more missions; the number is optional (auto: M010, M020, ... M990), e.g. `Drive M500Wave` (`M000`/`M999` are reserved) |
| `raccoon missions list` | List the project's missions |
| `raccoon missions rename <name\|Mxxx> <new-name>` | Rename a mission (file, class and references; the M-number is kept) |
| `raccoon missions remove <name\|Mxxx>... [--keep-file]` | Remove one or more missions |
| `raccoon missions reorder [order...]` | Reorder missions (interactive without arguments) and renumber them |

### Hardware & code generation

| Command | Description |
|:--------|:------------|
| `raccoon codegen` | Regenerate `src/hardware/defs.py` and `src/hardware/robot.py` from config |
| `raccoon calibrate ticks` | Measure encoder ticks per revolution |
| `raccoon calibrate autotune` | Run motor autotune |
| `raccoon calibrate servos` | Interactively calibrate servo offsets |
| `raccoon calibrate step-response` | Record a motor step response |
| `raccoon validate` | Check config, files and imports for consistency |
| `raccoon migrate` | Apply numbered project migrations |

### Remote development

| Command | Description |
|:--------|:------------|
| `raccoon connect <address>` | Connect to a Pi server |
| `raccoon disconnect` | Disconnect from the current Pi |
| `raccoon status` | Show connection status |
| `raccoon sync` | Sync changed files to the Pi (uses content hashing) |
| `raccoon sync --force` | Re-upload all files |
| `raccoon run` | Regenerate code, sync, and run on the Pi |
| `raccoon upload` | Full preflight, then push to the robot and confirm it starts |
| `raccoon shell` | Open an SSH session on the connected Pi |
| `raccoon doctor` | Health check of tools, connection and versions |

### Debugging

| Command | Description |
|:--------|:------------|
| `raccoon logs show` / `tail` / `download` / `clear` | Read, follow, download or clear the robot's logs (`raccoon logs sources` lists them) |
| `raccoon lcm spy` | Live-inspect messages on the bus |
| `raccoon lcm record <file>` | Record bus traffic to a file |
| `raccoon lcm list` / `playback` / `delete` / `status` | Manage and replay recordings |
| `raccoon checkpoint list` / `show` | List saved checkpoints or show one |
| `raccoon checkpoint restore <id>` | Restore project to a checkpoint |
| `raccoon checkpoint delete` / `clean` | Delete one checkpoint or clean up old ones |

### Tooling

| Command | Description |
|:--------|:------------|
| `raccoon update` | Update raccoon-cli to the latest version |
| `raccoon completion install` / `show` | Install or show shell tab-completion |
| `raccoon web` | Launch the web IDE |

---

## Project structure

A new project contains:

```
MyRobot/
├── raccoon.project.yml    # Hardware config -- motors, sensors, drivetrain, connection
├── src/
│   ├── main.py
│   ├── hardware/
│   │   ├── defs.py        # Generated -- do not edit by hand
│   │   └── robot.py       # Generated -- do not edit by hand
│   ├── missions/
│   │   ├── setup_mission.py
│   │   └── shutdown_mission.py
│   └── steps/
└── .raccoonignore         # match patterns excluded from sync
```

`defs.py` and `robot.py` are regenerated by `raccoon codegen` (and automatically on every `raccoon run`). Edit `raccoon.project.yml` to change the hardware config - never edit the generated files directly.

---

## Configuration

A project uses a split config layout. The root file just glues things together:

### `raccoon.project.yml`

```yaml
name: MyRobot
uuid: <auto-generated>

robot:       !include 'config/robot.yml'
missions:    !include 'config/missions.yml'
definitions: !include 'config/hardware.yml'
connection:  !include 'config/connection.yml'
```

### `config/hardware.yml` -- sensors, motors, servos

```yaml
button:
  type: DigitalSensor
  port: 10
imu:
  type: IMU
front_left_ir_sensor:
  type: IRSensor
  port: 1
front_right_ir_sensor:
  type: IRSensor
  port: 2

_motors: !include-merge 'motors.yml'
_servos: !include-merge 'servos.yml'
```

### `config/motors.yml`

```yaml
left_motor:
  type: Motor
  port: 0
  inverted: false
  calibration:
    ticks_to_rad: 0.00002   # set by raccoon calibrate
    vel_lpf_alpha: 1.0
    # Optional: omit pid to use STM32 firmware defaults
    # pid: {kp: 1.22, ki: 0.045, kd: 0.0}
right_motor:
  type: Motor
  port: 1
  inverted: true
  calibration:
    ticks_to_rad: 0.00002
    vel_lpf_alpha: 1.0
```

### `config/connection.yml`

```yaml
pi_address: 192.168.4.1
pi_port: 8421
pi_user: pi
auto_connect: true
```

### `~/.raccoon/config.yml` (global)

```yaml
known_pis:
  - hostname: raccoon-pi
    address: 192.168.4.1
default_pi_user: pi
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to get started contributing


## License

Copyright (C) 2026 Tobias Madlberger  
Licensed under the GNU General Public License v3.0 -- see [COPYING](COPYING) for details.
