# duct.py [![Build Status](https://travis-ci.org/oconnor663/duct.py.svg?branch=master)](https://travis-ci.org/oconnor663/duct.py) [![Build status](https://ci.appveyor.com/api/projects/status/5t3rq1xu5l38uaou/branch/master?svg=true)](https://ci.appveyor.com/project/oconnor663/duct-py/branch/master) [![Coverage Status](https://coveralls.io/repos/oconnor663/duct.py/badge.svg?branch=master&service=github)](https://coveralls.io/github/oconnor663/duct.py?branch=master) [![PyPI version](https://badge.fury.io/py/duct.svg)](https://pypi.python.org/pypi/duct) [![Documentation Status](https://readthedocs.org/projects/ductpy/badge/?version=latest)](https://ductpy.readthedocs.io/en/latest/?badge=latest)

Duct is a library for running child processes. It provides the control
and convenience of a shell, building pipelines and redirecting IO. At
the same time, Duct makes it easier to write correct code. Whitespace is
never significant, and errors from child processes become exceptions by
default. Duct also takes care of a surprising variety of [gotchas, bugs,
and platform
inconsistencies](https://github.com/oconnor663/duct.py/blob/master/gotchas.md),
to help simple programs do the right thing in tricky edge cases.

- [Documentation](https://ductpy.readthedocs.io)
- [PyPI package](https://pypi.python.org/pypi/duct)
- [the same library, in Rust](https://github.com/oconnor663/duct.rs)

Examples
--------

Run a command that writes to the terminal as usual:

```python
>>> from duct import cmd
>>> cmd("true").run()
Output(status=0, stdout=None, stderr=None)
```

Capture the output of a command:

```python
>>> cmd("echo", "hi").read()
'hi'
```

Capture the output of a pipeline:

```python
>>> cmd("echo", "hi").pipe(cmd("sed", "s/i/o/")).read()
'ho'
```

Merge stderr into stdout and read both incrementally:

```python
>>> reader = cmd("bash", "-c", "echo out && echo err 1>&2").stderr_to_stdout().reader()
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
