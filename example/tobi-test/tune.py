#!/usr/bin/env python3
"""
PID auto-tuner — runs on your laptop.

Uses Optuna to optimize heading PID params by repeatedly calling
`raccoon run` which syncs to the Pi, triggers codegen, runs a turn
trial, and syncs the results back.

Usage:
    python tune.py                     # 60 trials
    python tune.py --trials 100        # custom trial count
    python tune.py --angle 180         # tune for 180° turns
    python tune.py --resume            # resume previous study

Requirements (on laptop):
    pip install optuna pyyaml
"""

import argparse
import json
import subprocess
from pathlib import Path

import optuna

YAML_FILE = Path("raccoon.project.yml")
RESULTS_FILE = Path("tune_results.json")

PARAM_BOUNDS = {
    "kp": (0.5, 6.0),
    "ki": (0.0, 1.0),
    "kd": (0.1, 5.0),
    "derivative_lpf_alpha": (0.05, 0.8),
}


def update_yaml(kp: float, ki: float, kd: float, lpf: float) -> None:
    """Update heading PID params in raccoon.project.yml, preserving formatting."""
    lines = YAML_FILE.read_text().splitlines()

    section = None  # tracks: None | 'motion_pid' | 'heading'
    motion_pid_indent = -1
    heading_indent = -1

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)

        # Detect motion_pid section
        if stripped == "motion_pid:":
            section = "motion_pid"
            motion_pid_indent = indent
            continue

        # Left of motion_pid = exited
        if section and indent <= motion_pid_indent and stripped:
            section = None

        if section == "motion_pid":
            if stripped == "heading:":
                section = "heading"
                heading_indent = indent
                continue
            if stripped.startswith("derivative_lpf_alpha:"):
                lines[i] = f"{' ' * indent}derivative_lpf_alpha: {lpf}"

        if section == "heading":
            # Exited heading subsection
            if indent <= heading_indent and stripped:
                section = "motion_pid"
                # Re-check this line for derivative_lpf_alpha
                if stripped.startswith("derivative_lpf_alpha:"):
                    lines[i] = f"{' ' * indent}derivative_lpf_alpha: {lpf}"
                continue
            if stripped.startswith("kp:"):
                lines[i] = f"{' ' * indent}kp: {kp}"
            elif stripped.startswith("ki:"):
                lines[i] = f"{' ' * indent}ki: {ki}"
            elif stripped.startswith("kd:"):
                lines[i] = f"{' ' * indent}kd: {kd}"

    YAML_FILE.write_text("\n".join(lines) + "\n")


def run_trial() -> dict | None:
    """Call raccoon run and read back the results."""
    if RESULTS_FILE.exists():
        RESULTS_FILE.unlink()

    subprocess.run(["raccoon", "run"])
    subprocess.run(["raccoon", "sync", "--pull"])

    #
    # if proc.returncode != 0:
    #     print(f"  raccoon run failed (exit {proc.returncode})")
    #     return None

    if not RESULTS_FILE.exists():
        print("  No results file after run!")
        return None
    return json.loads(RESULTS_FILE.read_text())


def main():
    parser = argparse.ArgumentParser(description="Auto-tune heading PID (runs on laptop)")
    parser.add_argument("--trials", type=int, default=60)
    parser.add_argument("--angle", type=float, default=90.0)
    parser.add_argument("--resume", action="store_true", help="Resume previous study from DB")
    parser.add_argument("--db", type=str, default="tune_turn.db")
    args = parser.parse_args()

    study = optuna.create_study(
        study_name="turn_pid_tuning",
        direction="minimize",
        storage=f"sqlite:///{args.db}",
        load_if_exists=args.resume,
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    prev = len(study.trials)
    print(f"Target:  {args.angle}°")
    print(f"Bounds:  {PARAM_BOUNDS}")
    print(f"Trials:  {args.trials} (+ {prev} previous)")
    print()

    def objective(trial: optuna.Trial) -> float:
        kp = trial.suggest_float("kp", *PARAM_BOUNDS["kp"])
        ki = trial.suggest_float("ki", *PARAM_BOUNDS["ki"])
        kd = trial.suggest_float("kd", *PARAM_BOUNDS["kd"])
        lpf = trial.suggest_float("derivative_lpf_alpha", *PARAM_BOUNDS["derivative_lpf_alpha"])

        print(f"\n{'='*60}")
        print(f"[Trial {trial.number}] kp={kp:.4f} ki={ki:.4f} kd={kd:.4f} lpf={lpf:.4f}")
        print(f"{'='*60}")

        update_yaml(kp, ki, kd, lpf)
        result = run_trial()

        if result is None:
            return 1000.0

        cost = result["cost"]
        status = "OK" if result["completed"] else "TIMEOUT"
        print(
            f"  {status} | {result['elapsed_time']:.2f}s "
            f"err={abs(result['final_error_deg']):.2f}° "
            f"overshoot={result['max_overshoot_deg']:.1f}° "
            f"osc={result['oscillation_count']} "
            f"cost={cost:.3f}"
        )
        return cost

    try:
        study.optimize(objective, n_trials=args.trials)
    except KeyboardInterrupt:
        print("\n\nInterrupted — results saved to DB.")

    if not study.trials:
        print("No completed trials.")
        return

    best = study.best_trial
    print(f"\n{'='*60}")
    print(f"BEST (trial #{best.number}, cost={best.value:.4f})")
    print(f"{'='*60}")
    print(f"  heading:")
    print(f"    kp: {best.params['kp']:.4f}")
    print(f"    ki: {best.params['ki']:.4f}")
    print(f"    kd: {best.params['kd']:.4f}")
    print(f"  derivative_lpf_alpha: {best.params['derivative_lpf_alpha']:.4f}")
    print()

    # Apply best params to YAML
    update_yaml(
        best.params["kp"],
        best.params["ki"],
        best.params["kd"],
        best.params["derivative_lpf_alpha"],
    )
    print("Best params written to raccoon.project.yml")


if __name__ == "__main__":
    main()
