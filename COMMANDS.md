# Raccoon CLI Commands

## Command Structure

The Raccoon CLI uses a hierarchical command structure:

- [**`raccoon create`**](#raccoon-create-project-name) - Create projects and missions
  - [`project`](#raccoon-create-project-name) - Create a new project
  - [`mission`](#raccoon-create-mission-name) - Create a new mission
- [**`raccoon list`**](#raccoon-list-projects) - List projects and missions
  - [`projects`](#raccoon-list-projects) - List all projects
  - [`missions`](#raccoon-list-missions) - List missions in current project
- [**`raccoon remove`**](#raccoon-remove-project-name) - Remove projects and missions
  - [`project`](#raccoon-remove-project-name) - Remove a project
  - [`mission`](#raccoon-remove-mission-name) - Remove a mission
- [**`raccoon calibrate`**](#raccoon-calibrate) - Calibrate robot motors
- [**`raccoon wizard`**](#raccoon-wizard) - Interactive project configuration
- [**`raccoon codegen`**](#raccoon-codegen) - Generate code from configuration
- [**`raccoon run`**](#raccoon-run) - Run the project

---

## Project Management Commands

### `raccoon create project <name>`

Creates a new Raccoon project with the specified name.

**Usage:**
```bash
raccoon create project MyRobot
raccoon create project MyRobot --path /path/to/parent/dir
raccoon create project MyRobot --wizard
```

**Options:**
- `--path PATH` - Directory to create the project in (default: current directory)
- `--wizard` - Run the configuration wizard after project creation

**What it does:**
1. Creates a new project directory with the given name
2. Generates project structure from templates (src/, missions/, hardware/, etc.)
3. Creates a `raccoon.project.yml` with a unique UUID
4. Sets up default configuration files
5. Optionally launches the setup wizard if `--wizard` flag is used

**Example:**
```bash
cd ~/my-projects
raccoon create project BotblockRobot
# This creates ~/my-projects/BotblockRobot/ with full project structure
# Run 'cd BotblockRobot && raccoon wizard' to configure later

# Or create with wizard immediately:
raccoon create project BotblockRobot --wizard
```

---

### `raccoon create mission <name>`

Creates a new mission in the current project.

**Usage:**
```bash
raccoon create mission CollectSamples
raccoon create mission navigate-to-goal  # Works with kebab-case too
raccoon create mission MyCustomMission
```

**What it does:**
1. Converts the name to proper snake_case and PascalCase formats
2. Creates a new mission file in `src/missions/<mission_name>_mission.py`
3. Adds the mission to the `missions` list in `raccoon.project.yml`
4. Adds the import statement to `src/main.py`

**Smart Name Handling:**
- Automatically detects and removes "Mission" suffix if accidentally included
- Example: `CollectSamplesMission` → automatically corrected to `CollectSamples`
- Prevents duplicate suffix (e.g., `CollectSamplesMissionMission`)

**Example:**
```bash
cd BotblockRobot
raccoon create mission CollectSamples
# Creates: src/missions/collect_samples_mission.py
# Adds: CollectSamplesMission to raccoon.project.yml
# Imports: from .missions.collect_samples_mission import CollectSamplesMission

# If you accidentally include "Mission" suffix, it will be auto-corrected:
raccoon create mission NavigateToGoalMission
# Note: Removed 'Mission' suffix from name.
#   Input: 'NavigateToGoalMission' → Using: 'NavigateToGoal'
# Creates: NavigateToGoalMission (not NavigateToGoalMissionMission)
```

---

### `raccoon remove mission <name>`

Removes a mission from the current project.

**Usage:**
```bash
raccoon remove mission CollectSamples
raccoon remove mission CollectSamples --keep-file
```

**Options:**
- `--keep-file` - Keep the mission file, only remove from configuration

**What it does:**
1. Removes the mission from the `missions` list in `raccoon.project.yml`
2. Removes the import statement from `src/main.py`
3. Deletes the mission file (unless `--keep-file` is specified)

**Example:**
```bash
cd BotblockRobot
raccoon remove mission CollectSamples
# Removes mission from config and deletes the file

raccoon remove mission TestMission --keep-file
# Removes from config but keeps the file for reference
```

---

### `raccoon list projects`

Lists all Raccoon projects in a specified directory.

**Usage:**
```bash
raccoon list projects
raccoon list projects --path /path/to/search
```

**Options:**
- `--path PATH` - Directory to search for projects (default: current directory)

**What it does:**
1. Searches the specified directory and its immediate subdirectories
2. Finds all directories containing `raccoon.project.yml`
3. Displays a table with project information
4. Shows mission count for each project

**Example Output:**
```
Searching for projects in: /home/user/projects

                  Raccoon Projects                   
┏━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ # ┃ Project Name   ┃ Location        ┃ Missions ┃
┡━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ 1 │ BotblockRobot  │ ./BotblockRobot │        3 │
│ 2 │ TestProject    │ ./TestProject   │        1 │
└───┴────────────────┴─────────────────┴──────────┘

Total: 2 project(s)
```

---

### `raccoon list missions`

Lists all missions configured in the current project.

**Usage:**
```bash
cd MyRobot
raccoon list missions
```

**What it does:**
1. Displays project name and location
2. Shows a table with all missions from `raccoon.project.yml`
3. Checks if each mission file exists in `src/missions/`
4. Indicates status with ✓ (exists) or ✗ (missing)

**Example Output:**
```
Project: MyRobot
Location: /path/to/MyRobot

                          Missions                           
┏━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃  # ┃ Mission Class        ┃ File                      ┃ Status  ┃
┡━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│  1 │ CollectBlocksMission │ collect_blocks_mission.py │ ✓ Exists│
│  2 │ DeliverToZoneMission │ deliver_to_zone_mission.py│ ✓ Exists│
│  3 │ ReturnHomeMission    │ return_home_mission.py    │ ✗ Missing│
└────┴──────────────────────┴───────────────────────────┴─────────┘

Total: 3 mission(s)
```

**Use cases:**
- Quick overview of all missions in your project
- Verify mission files are correctly named and exist
- Detect configuration/file mismatches

---

### `raccoon remove project <name>`

Removes a project and all its files.

**Usage:**
```bash
raccoon remove project MyOldProject
raccoon remove project MyProject --path /path/to/parent
raccoon remove project MyProject --force  # Skip confirmation
```

**Options:**
- `--path PATH` - Directory containing the project (default: current directory)
- `--force` - Skip confirmation prompt

**What it does:**
1. Verifies the project exists and is a valid Raccoon project
2. Displays project information
3. Asks for confirmation (unless `--force` is used)
4. Deletes the entire project directory

**Example:**
```bash
raccoon remove project OldRobot
# ⚠ About to delete project:
#   Name: OldRobot
#   Path: /home/user/projects/OldRobot
# 
# Are you sure you want to delete this project? This cannot be undone. [y/N]: y
# ✓ Project 'OldRobot' deleted successfully
```

⚠️ **Warning:** This command permanently deletes the project directory and cannot be undone!

---

### `raccoon calibrate`

Calibrate robot motors to determine optimal PID and feedforward parameters.

**Usage:**
```bash
cd MyRobot
raccoon calibrate
raccoon calibrate --aggressive  # Use aggressive calibration mode
```

**Options:**
- `--aggressive` - Use aggressive calibration mode (relay feedback) for more precise tuning

**What it does:**
1. Initializes the robot from your generated hardware configuration
2. Runs motor calibration routines to determine:
   - PID parameters (kp, ki, kd)
   - Feedforward parameters (kS, kV, kA)
3. Displays calibration results in a formatted table
4. Saves the calibration data to `raccoon.project.yml`

**Requirements:**
- Must be run from within a project directory
- Requires `raccoon codegen` to have been run first
- Robot hardware must be connected and accessible

**Example:**
```bash
cd BotblockRobot
raccoon codegen  # Generate robot code first
raccoon calibrate

# Starting Motor Calibration
# Mode: Standard
# Project: BotblockRobot
#
# Running calibration... This may take a few moments.
#
# ┌──────────────────────────────────────────────────────────┐
# │              Motor Calibration Results                   │
# ├────────────────┬──────────┬─────────────┬────────────────┤
# │ Motor          │ Status   │ PID         │ Feedforward    │
# ├────────────────┼──────────┼─────────────┼────────────────┤
# │ front_left...  │ ✓ Success│ kp=4.4554   │ kS=0.024996    │
# │                │          │ ki=10.0065  │ kV=0.041656    │
# │                │          │ kd=0.1653   │ kA=0.007958    │
# └────────────────┴──────────┴─────────────┴────────────────┘
#
# Save calibration results to raccoon.project.yml? [Y/n]: y
# ✓ Calibration results saved to raccoon.project.yml
```

**Calibration Modes:**
- **Standard** (default): Uses standard calibration routines suitable for most robots
- **Aggressive** (`--aggressive`): Uses relay feedback method for more aggressive tuning, may provide better results but requires more careful monitoring

**Updated YAML Format:**
After calibration, each motor in your `raccoon.project.yml` will have a `calibration` section:
```yaml
rear_right_motor:
  type: "Motor"
  port: 3
  inverted: false
  calibration:
    ff:
      kS: 0.024995811866685317
      kV: 0.04165603596837704
      kA: 0.007957684774524531
    pid:
      kp: 4.455453994623986
      ki: 10.006495754164112
      kd: 0.1653181991803507
    ticks_to_rad: 0.00418879
    vel_lpf_alpha: 0.8
```

**Calibration Subcommands:**

The `raccoon calibrate` command has several subcommands for different calibration tasks:

- `raccoon calibrate motors` - Calibrate motor PID and feedforward parameters
- `raccoon calibrate rpm` - Calibrate motor RPM vs power using hall effect sensor
- `raccoon calibrate deadzone` - Interactive deadzone calibration using human observation
- `raccoon calibrate benchmark` - Test motor PID responsiveness and control quality
- `raccoon calibrate maxspeed` - Determine maximum motor speeds by testing at full power

#### `raccoon calibrate maxspeed`

Determines the maximum speed of all motors by running them at full power (100% and -100%) for a specified duration and measuring average speed from Back-EMF feedback.

**Usage:**
```bash
raccoon calibrate maxspeed
raccoon calibrate maxspeed --duration 5.0
raccoon calibrate maxspeed --local --yes
```

**Options:**
- `-d, --duration FLOAT` - Test duration in seconds per direction (default: 10.0)
- `-l, --local` - Run locally on this machine (requires hardware)
- `-y, --yes` - Auto-save calibration results without prompting

**What it does:**
1. Discovers all motors defined in `raccoon.project.yml`
2. For each motor:
   - Runs at 100% power for specified duration (forward test)
   - Measures average speed from Back-EMF encoder
   - Runs at -100% power for specified duration (reverse test)
   - Measures average speed in reverse
3. Converts speeds from ticks/second to rad/s using `ticks_to_rad`
4. Saves results to each motor's calibration section

**Results saved to YAML:**
```yaml
front_left_motor:
  calibration:
    max_forward_speed: 5.24  # rad/s
    max_reverse_speed: 5.18  # rad/s
```

**Example output:**
```
┌────────────────────┬──────┬──────────────────┬──────────────────┬───────┐
│ Motor              │ Port │ Forward (rad/s)  │ Reverse (rad/s)  │ Status│
├────────────────────┼──────┼──────────────────┼──────────────────┼───────┤
│ front_left_motor   │ 0    │ 5.24             │ 5.18             │   ✓   │
│ front_right_motor  │ 1    │ 5.31             │ 5.29             │   ✓   │
│ rear_left_motor    │ 2    │ 5.22             │ 5.20             │   ✓   │
│ rear_right_motor   │ 3    │ 5.28             │ 5.25             │   ✓   │
└────────────────────┴──────┴──────────────────┴──────────────────┴───────┘
```

---

### `raccoon wizard`

Interactive wizard to configure `raccoon.project.yml`.

**Usage:**
```bash
cd MyRobot
raccoon wizard
raccoon wizard --dry-run  # Preview without saving
```

**What it does:**
- Prompts for drivetrain type (mecanum/differential)
- Collects motor port and inversion settings
- Gathers robot measurements (wheel diameter, track width, etc.)
- Optionally runs encoder calibration
- Updates `raccoon.project.yml` with the configuration

---

### `raccoon codegen`

Generate Python code from your `raccoon.project.yml` configuration.

**Usage:**
```bash
cd MyRobot
raccoon codegen
raccoon codegen --only defs         # Generate only defs.py
raccoon codegen --only robot        # Generate only robot.py
raccoon codegen --only defs --only robot  # Generate both
raccoon codegen --no-format         # Skip code formatting
raccoon codegen -o custom/path      # Custom output directory
```

**Options:**
- `--only TEXT` - Generate specific file(s) only (`defs`, `robot`). Can be specified multiple times.
- `--no-format` - Skip black code formatting
- `-o, --output-dir PATH` - Override output directory (default: `src/hardware/`)

**What it does:**
1. Reads your `raccoon.project.yml` configuration
2. Generates Python code for your robot hardware:
   - **`defs.py`** - Hardware definitions (motors, sensors, etc.)
   - **`robot.py`** - Robot class with drive, odometry, and kinematics
3. Formats the generated code with Black (unless `--no-format` is used)
4. Displays a summary of generated files

**Requirements:**
- Must be run from within a project directory
- Requires a valid `raccoon.project.yml` configuration file

**Example Output:**
```bash
cd BotblockRobot
raccoon codegen

# ╭────────────────────────────────────────────────────╮
# │       Code generation complete                     │
# │   Output: src/hardware | Formatting: on            │
# │ ┌───────────┬─────────────────────────────────┐   │
# │ │ Generator │ File                            │   │
# │ ├───────────┼─────────────────────────────────┤   │
# │ │ defs      │ src/hardware/defs.py            │   │
# │ │ robot     │ src/hardware/robot.py           │   │
# │ └───────────┴─────────────────────────────────┘   │
# ╰────────────────────────────────────────────────────╯
```

**Generated Files:**

**`src/hardware/defs.py`** - Contains hardware component definitions:
```python
# Auto-generated motor definitions
front_left_motor = Motor(port=0, inverted=False, ticks_to_rad=0.00418879, vel_lpf_alpha=0.8)
front_right_motor = Motor(port=1, inverted=True, ticks_to_rad=0.00418879, vel_lpf_alpha=0.8)
# ... more components
imu = IMU()
```

**`src/hardware/robot.py`** - Contains the Robot class:
```python
class Robot:
    def __init__(self):
        self.drive = MecanumDrive(...)
        self.odometry = FusedOdometry(...)
        # ... initialization code
```

**Use Cases:**
- **Initial setup**: Generate hardware classes after running the wizard
- **Configuration changes**: Regenerate after modifying `raccoon.project.yml`
- **Selective generation**: Use `--only` to regenerate specific files during development
- **CI/CD**: Integrate into build pipelines with `--no-format` for faster execution

**When to run:**
- After creating a new project
- After running `raccoon wizard`
- After manually editing `raccoon.project.yml`
- Before running `raccoon calibrate` or `raccoon run`

---

### `raccoon run`

Run code generation and then execute your robot's main program.

**Usage:**
```bash
cd MyRobot
raccoon run
raccoon run --arg1 value1 --arg2 value2  # Pass arguments to src.main
```

**Arguments:**
- `[ARGS]...` - Any arguments passed will be forwarded to `src.main`

**What it does:**
1. Automatically runs `raccoon codegen` to ensure code is up-to-date
2. Formats the generated code with Black
3. Executes `python -m src.main` with any provided arguments
4. Displays the exit code and status

**Requirements:**
- Must be run from within a project directory
- Requires a valid `raccoon.project.yml` configuration file
- Requires a `src/main.py` file with proper entry point

**Example:**
```bash
cd BotblockRobot
raccoon run

# Running in project: /path/to/BotblockRobot
# Reading config from raccoon.project.yml
# Code generation complete
# Running src.main...
# 
# [Your program output here]
# 
# ╭─────────────────────────────────────╮
# │ src.main exited with code 0         │
# ╰─────────────────────────────────────╯
```

**Passing Arguments:**
```bash
# Pass mission name to your program
raccoon run --mission CollectSamples

# Pass multiple arguments
raccoon run --debug --mission TestMission --timeout 30
```

**Exit Codes:**
- **0** - Program completed successfully (green panel)
- **Non-zero** - Program encountered an error (red panel)

**Workflow Integration:**
This command is ideal for rapid development:
```bash
# Edit raccoon.project.yml or mission code
vim src/missions/collect_samples_mission.py

# Run automatically regenerates hardware code and executes
raccoon run

# No need to manually run codegen first!
```

**Difference from direct execution:**
```bash
# Manual approach (3 steps):
raccoon codegen
cd src
python -m main

# raccoon run (1 step, auto-regenerates):
raccoon run
```

**Use Cases:**
- **Development**: Quick iteration during mission development
- **Testing**: Test missions on actual hardware
- **Debugging**: Run with debug arguments passed through
- **Competition**: Execute competition missions with specific parameters

---

## Name Format Handling

The commands intelligently handle different naming formats:

| Input Format      | Mission Class           | File Name                     | 
|-------------------|-------------------------|-------------------------------|
| `CollectSamples`  | `CollectSamplesMission` | `collect_samples_mission.py`  |
| `collect-samples` | `CollectSamplesMission` | `collect_samples_mission.py`  |
| `collect_samples` | `CollectSamplesMission` | `collect_samples_mission.py`  |
| `NavigateToGoal`  | `NavigateToGoalMission` | `navigate_to_goal_mission.py` |

---

## Workflow Example

```bash
# 1. Create a new project
raccoon create project BotblockRobot

# 2. Navigate to project and configure
cd BotblockRobot
raccoon wizard

# 3. Create missions for competition tasks
raccoon create mission CollectBlock
raccoon create mission DeliverToZone
raccoon create mission ReturnHome

# 4. List all missions to verify
raccoon list missions

# 5. Remove a mission if not needed
raccoon remove mission ReturnHome

# 6. List all projects to see what you have
cd ..
raccoon list projects

# 7. Go back and run codegen to generate hardware classes
cd BotblockRobot
raccoon codegen

# 8. Calibrate the robot motors
raccoon calibrate

# 9. Test your missions
raccoon run
```

---

## Project Structure

After running `raccoon create project MyRobot`, you'll have:

```
MyRobot/
├── raccoon.project.yml    # Main configuration file
├── config.yaml            # Runtime configuration
├── project.yaml           # Project metadata
├── run.sh                 # Script to run locally
├── upload.sh              # Script to upload to robot
├── .gitignore
└── src/
    ├── __init__.py
    ├── main.py            # Entry point
    ├── hardware/
    │   ├── __init__.py
    │   └── defs.py        # Hardware definitions (generated)
    ├── missions/
    │   ├── __init__.py
    │   ├── setup_mission.py
    │   └── shutdown_mission.py
    └── steps/
        └── __init__.py
```

---

## Dependencies

The new commands require:
- `jinja2>=3.0` - Template rendering
- `jinja2-time>=0.2` - Time extensions for templates

These are automatically installed with the Raccoon toolchain.

