# Raccoon CLI Commands

## Command Structure

The Raccoon CLI uses a hierarchical command structure:

- **`raccoon create`** - Create projects and missions
  - `project` - Create a new project
  - `mission` - Create a new mission
- **`raccoon list`** - List projects and missions
  - `projects` - List all projects
  - `missions` - List missions in current project
- **`raccoon remove`** - Remove projects and missions
  - `project` - Remove a project
  - `mission` - Remove a mission
- **`raccoon wizard`** - Interactive project configuration
- **`raccoon codegen`** - Generate code from configuration
- **`raccoon run`** - Run the project

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

## Name Format Handling

The commands intelligently handle different naming formats:

| Input Format | Mission Class | File Name | 
|--------------|---------------|-----------|
| `CollectSamples` | `CollectSamplesMission` | `collect_samples_mission.py` |
| `collect-samples` | `CollectSamplesMission` | `collect_samples_mission.py` |
| `collect_samples` | `CollectSamplesMission` | `collect_samples_mission.py` |
| `NavigateToGoal` | `NavigateToGoalMission` | `navigate_to_goal_mission.py` |

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

# 8. Test your missions
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

