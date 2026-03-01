# Raccoon

A toolchain CLI for Botball robot development on Raspberry Pi (Wombat). Raccoon streamlines project creation, hardware configuration, code generation, motor calibration, and remote development.

## Features

- **Project Scaffolding** - Create new robot projects with proper structure and configuration
- **Interactive Hardware Wizard** - Configure motors, sensors, and drivetrain through guided prompts
- **Code Generation** - Generate Python hardware classes from YAML configuration
- **Motor Calibration** - Determine optimal PID and feedforward parameters for your motors
- **Remote Development** - Develop on your laptop and run on the Pi with automatic file sync
- **Web IDE** - Browser-based development environment

## Installation

### On Your Laptop (Development Machine)

```bash
pip install raccoon
```

### On Raspberry Pi (Robot)

```bash
pip install raccoon

# Install and start the server daemon
sudo raccoon-server install
```

### Development Installation

```bash
git clone <repository-url>
cd toolchain
pip install -e .
```

## Quick Start

### Create a New Project

```bash
raccoon create project MyRobot
cd MyRobot
```

### Configure Hardware

```bash
raccoon wizard
```

The wizard will guide you through:
- Drivetrain type (mecanum or differential)
- Motor ports and inversion settings
- Robot measurements (wheel diameter, track width)

### Generate Hardware Code

```bash
raccoon codegen
```

This generates `src/hardware/defs.py` and `src/hardware/robot.py` from your `raccoon.project.yml` configuration.

### Calibrate Motors

```bash
raccoon calibrate
```

Runs calibration routines to determine PID and feedforward parameters, saving results to your project config.

### Run Your Project

```bash
raccoon run
```

Automatically regenerates code and executes `src/main.py`.

## Remote Development

Develop on your laptop and run on the Raspberry Pi:

### 1. Connect to Your Pi

```bash
raccoon connect 192.168.4.1
```

### 2. Sync and Run

```bash
raccoon run  # Auto-syncs files and runs on Pi
```

### 3. Manual Sync

```bash
raccoon sync           # Sync changed files
raccoon sync --force   # Re-upload all files
```

See [Remote Development Guide](docs/REMOTE_DEVELOPMENT.md) for detailed setup instructions.

## Project Structure

After creating a project:

```
MyRobot/
├── raccoon.project.yml    # Main configuration (hardware, motors, connection)
├── src/
│   ├── main.py            # Entry point
│   ├── hardware/
│   │   ├── defs.py        # Generated hardware definitions
│   │   └── robot.py       # Generated robot class
│   ├── missions/
│   │   ├── setup_mission.py
│   │   └── shutdown_mission.py
│   └── steps/
└── ...
```

## Commands Reference

| Command | Description |
|---------|-------------|
| `raccoon create project <name>` | Create a new project |
| `raccoon create mission <name>` | Add a mission to current project |
| `raccoon list projects` | List all projects in directory |
| `raccoon list missions` | List missions in current project |
| `raccoon remove mission <name>` | Remove a mission |
| `raccoon wizard` | Interactive hardware configuration |
| `raccoon codegen` | Generate code from configuration |
| `raccoon calibrate` | Calibrate motor parameters |
| `raccoon run` | Run the project |
| `raccoon connect <address>` | Connect to a Pi server |
| `raccoon sync` | Sync files to connected Pi |
| `raccoon status` | Show connection status |
| `raccoon disconnect` | Disconnect from Pi |
| `raccoon web` | Launch web IDE |

See [COMMANDS.md](COMMANDS.md) for detailed command documentation.

## Configuration

### Project Configuration (`raccoon.project.yml`)

```yaml
name: MyRobot
uuid: unique-identifier
missions:
  - SetupMission
  - ShutdownMission

drivetrain_type: mecanum  # or differential

motors:
  front_left_motor:
    type: Motor
    port: 0
    inverted: false
    calibration:
      pid: {kp: 4.4, ki: 10.0, kd: 0.165}
      ff: {kS: 0.024, kV: 0.041, kA: 0.007}

connection:
  pi_address: 192.168.4.1
  pi_port: 8421
  pi_user: pi
```

### Global Configuration (`~/.raccoon/config.yml`)

```yaml
known_pis:
  - hostname: raccoon-pi
    address: 192.168.4.1
default_pi_user: pi
```

## Contributing

### Development Setup

```bash
git clone <repository-url>
cd toolchain
pip install -e .
```

### Running Tests

```bash
pytest tests/
pytest tests/test_sftp_sync.py -v  # Specific test file
```

### Project Layout

```
raccoon/
├── cli.py              # Main CLI entry point (Click)
├── project.py          # Project discovery and validation
├── commands/           # CLI command implementations
├── codegen/            # Code generation system
│   ├── pipeline.py     # Generation orchestrator
│   └── generators/     # Pluggable generators
├── client/             # Laptop-side (SSH, SFTP, HTTP client)
├── server/             # Pi-side FastAPI daemon
└── templates/          # Jinja2 project templates
```

### Key Concepts

- **Generators** are pluggable modules in `codegen/generators/` that transform YAML config into Python code
- **SFTP Sync** uses content hashing to efficiently sync only changed files
- **Client-Server** architecture separates laptop CLI from Pi execution daemon

### Code Style

- Use [Black](https://black.readthedocs.io/) for formatting
- Follow existing patterns in the codebase

## Requirements

- Python 3.8+
- Raspberry Pi with Botball Wombat controller (for robot execution)

## License

[Add license information]
