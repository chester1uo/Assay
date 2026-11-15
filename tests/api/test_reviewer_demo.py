"""The anonymous demo keeps separate guest workspaces and preserves main sign-in."""
import os
import subprocess
import sys
from pathlib import Path



def test_demo_guest_workspaces_and_cookie_isolation(tmp_path):
    # The demo intentionally changes module globals in its dedicated process.
    # Exercise it in a subprocess so other account tests remain unaffected.
    scenario = Path(__file__).with_name("reviewer_demo_scenario.py")
    result = subprocess.run([sys.executable, str(scenario), str(tmp_path)],
                            capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
