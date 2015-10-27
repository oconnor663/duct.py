# duct [![Build Status](https://travis-ci.org/oconnor663/duct.svg?branch=master)](https://travis-ci.org/oconnor663/duct) [![Coverage Status](https://coveralls.io/repos/oconnor663/duct/badge.svg?branch=master&service=github)](https://coveralls.io/github/oconnor663/duct?branch=master)

A Python library for shelling out. Goals:

- Finally let me stop using bash. That means supporting everything that
  bash can do, even though usually I don't need it.

  ```bash
  (echo dizzle && echo dazzle) | sed s/d/sn/ >&2
  ```

  becomes

  ```python
  cmd('echo', 'dizzle').then('echo', 'dazzle').pipe('sed', 's/d/sn/').run(stdout=STDERR)
  ```
- Default to the safe, correct way of doing things. *Errors should never
  pass silently.* Expect whitespace. Make buffer deadlocks impossible.
  Bash's `set -e -u -o pipefail` is the default.
- Make short things short: `output = sh("echo hello world").read()`
- Integrate closely with pathlib. Pathlib is amazing.
