"""
wisprflow_sdk/_installer.py

Entry point for the `wisprflow-patch` command.
Explains what the patch does, asks for consent, then runs patch_wispr.ps1.
"""

import subprocess
import sys
from pathlib import Path

GITHUB_URL = "https://github.com/ThisisShashwat/wisprflow-sdk/blob/main/TECHNICAL_DETAILS.md"

SUMMARY = """
┌─────────────────────────────────────────────────────────────────┐
│                     wisprflow-patch                             │
└─────────────────────────────────────────────────────────────────┘

This tool patches your local Wispr Flow installation so the SDK
can read the runtime config it needs (model ID, API endpoint, key).

Here's what it does — nothing more, nothing less:

  1. Finds your installed Wispr Flow app folder
  2. Closes Wispr Flow if it's running
  3. Creates a backup of the original app file (app.asar.backup)
     — you can restore this manually at any time
  4. Extracts the app, injects a small config-logger into one JS file,
     and repacks it — no network calls, no data sent anywhere
  5. On your next dictation in Wispr Flow, a file called
     wispr_runtime.json is written to your local AppData folder
     — the SDK reads that file to connect to the backend

The patch is reversible. Your backup is saved next to the original.
If something goes wrong, the script restores the backup automatically.

You will need to run this everytime the Wispr Flow app updates.

For the full technical breakdown, see:
  {github_url}

""".format(github_url=GITHUB_URL)


def _confirm() -> bool:
    while True:
        try:
            answer = input("Apply the patch? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("Please enter y or n.")


def run_patch():
    if sys.platform != "win32":
        print("wisprflow-patch only works on Windows.")
        print("Wispr Flow is a Windows-only application.")
        sys.exit(1)

    ps1 = Path(__file__).parent / "patch_wispr.ps1"
    if not ps1.exists():
        print(f"ERROR: patch script not found at {ps1}")
        print("Try reinstalling: pip install --force-reinstall wisprflow-sdk")
        sys.exit(1)

    print(SUMMARY)

    if not _confirm():
        print("\nAborted. Nothing was changed.")
        sys.exit(0)

    print()

    result = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
        check=False,
    )

    if result.returncode != 0:
        print(f"\nPatch exited with an error (code {result.returncode}).")
        print("If Wispr Flow updated recently, re-run wisprflow-patch.")
        print(f"For help: {GITHUB_URL}")
        sys.exit(result.returncode)

    print("\nAll done! Open Wispr Flow and do one dictation.")
    print("That writes wispr_runtime.json, and the SDK will be ready to use.")


if __name__ == "__main__":
    run_patch()