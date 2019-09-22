# duct.py [![Build Status](https://travis-ci.org/oconnor663/duct.py.svg?branch=master)](https://travis-ci.org/oconnor663/duct.py) [![Build status](https://ci.appveyor.com/api/projects/status/5t3rq1xu5l38uaou/branch/master?svg=true)](https://ci.appveyor.com/project/oconnor663/duct-py/branch/master) [![Coverage Status](https://coveralls.io/repos/oconnor663/duct.py/badge.svg?branch=master&service=github)](https://coveralls.io/github/oconnor663/duct.py?branch=master) [![PyPI version](https://badge.fury.io/py/duct.svg)](https://pypi.python.org/pypi/duct) [![Documentation Status](https://readthedocs.org/projects/ductpy/badge/?version=latest)](https://ductpy.readthedocs.io/en/latest/?badge=latest)

Duct is a library for running child processes. Duct makes it easy to
build pipelines and redirect IO like a shell. At the same time, Duct
helps you write correct, portable code: whitespace is never significant,
errors from child processes get reported by default, and a variety of
[gotchas, bugs, and platform
inconsistencies](https://github.com/oconnor663/duct.py/blob/master/gotchas.md)
are handled for you the Right Way™.

- [Documentation](https://ductpy.readthedocs.io)
- [PyPI package](https://pypi.python.org/pypi/duct)
- [the same library, in Rust](https://github.com/oconnor663/duct.rs)

Changelog
---------

- v0.6.0
  - Removed the `sh` function.
  - Removed the `then` method.
  - Add `Handle.kill`.
  - Add `ReaderHandle` and `Expression.reader()`.
  - Rename `stdin`/`stdout`/`stderr` to
    `stdin_path`/`stdout_path`/`stderr_path`.
  - This will be the last major release supporting Python 2.

Examples
--------

Run a command without capturing any output. Here "hi" is printed directly to
the terminal:

```python
>>> from duct import cmd
>>> cmd("echo", "hi").run()
hi
Output(status=0, stdout=None, stderr=None)
```

Capture the standard output of a command. Here "hi" is returned as a string:

```python
>>> cmd("echo", "hi").read()
'hi'
```

Capture the standard output of a pipeline:

```python
>>> cmd("echo", "hi").pipe(cmd("sed", "s/i/o/")).read()
'ho'
```

Merge standard error into standard output and read both incrementally:

```python
>>> big_cmd = cmd("bash", "-c", "echo out && echo err 1>&2")
>>> reader = big_cmd.stderr_to_stdout().reader()
>>> with reader:
...     reader.readlines()
[b'out\n', b'err\n']
```

Children that exit with a non-zero status raise an exception by default:

```python
>>> cmd("false").run()
Traceback (most recent call last):
...
duct.StatusError: Expression cmd('false') returned non-zero exit status: Output(status=1, stdout=None, stderr=None)
>>> cmd("false").unchecked().run()
Output(status=1, stdout=None, stderr=None)
```
