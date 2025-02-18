import os
import subprocess
import sys

pytest_cmd = [
    sys.executable,
    "-m",
    "pytest",
    "duct.py",
    "test_duct.py",
    "--verbose",
]

# Doctests are only compatible with Python 3 and non-Windows.
if sys.version_info.major > 2 and os.name != "nt":
    pytest_cmd.append("--doctest-modules")

print("Executing:", " ".join(pytest_cmd))
subprocess.check_call(pytest_cmd)

print("Executing: flake8")
subprocess.check_call(["flake8", "--max-line-length=88"])

print("Executing: black --check")
subprocess.check_call(
    ["black", "--check", "duct.py", "test_duct.py", "setup.py", "ci.py"]
)

print("Success!")
