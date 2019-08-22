# duct.py [![Build Status](https://travis-ci.org/oconnor663/duct.py.svg?branch=master)](https://travis-ci.org/oconnor663/duct.py) [![Build status](https://ci.appveyor.com/api/projects/status/5t3rq1xu5l38uaou/branch/master?svg=true)](https://ci.appveyor.com/project/oconnor663/duct-py/branch/master) [![Coverage Status](https://coveralls.io/repos/oconnor663/duct.py/badge.svg?branch=master&service=github)](https://coveralls.io/github/oconnor663/duct.py?branch=master) [![PyPI version](https://badge.fury.io/py/duct.svg)](https://pypi.python.org/pypi/duct)


A Python library for shelling out. One of the goals of duct is to be
easily portable to other languages, and there's a [Rust
version](https://github.com/oconnor663/duct.rs) happening in parallel.

[PyPI package](https://pypi.python.org/pypi/duct/)

## Easy things should be easy.

But always be explicit about what happens to output.

```python
from duct import cmd

# Read the name of the current git branch.
current_branch = cmd("git", "symbolic-ref", "--short", "HEAD").read()

# Log the current branch, with git taking over the terminal as usual.
cmd("git", "log", current_branch).run()
```

That's exactly the same as the following in standard Python 3.5:

```python
from subprocess import run, PIPE

result = run(["git", "symbolic-ref", "--short", "HEAD"], stdout=PIPE,
             universal_newlines=True, check=True)
current_branch = result.stdout.rstrip("\n")

run(["git", "log", current_branch], check=True)
```


## Fancy things should be possible.

Sometimes you have to write complicated pipelines in bash:

```bash
foo 2>&1 | (cat || true) | head -n 10
```

The duct version is longer, but duct expressions are composable objects,
so we can build the whole command piece-by-piece:

```python
from duct import cmd

foo = cmd("foo").stdout_to_stderr()
cat = cmd("cat").unchecked()
head = cmd("head", "-n", "10")
foo.pipe(cat).pipe(head).run()
```


## Errors should never pass silently.

If a command returns an error code, duct will raise an exception:

```python
cmd("false").run()  # Raises an exception.
```

To ignore the error, you can use `unchecked`:

```python
result = cmd("false").unchecked().run()
assert result.status == 1
```

Note that duct treats errors in a pipe like bash's `pipefail` option:
they count even when they happen on the left. This can be surprising in
cases where we usually ignore errors. In the following example, `cat`
returns an error because its stdout is closed:

```python
# Raises an exception, because cat returns an error.
cmd("cat").stdin("/dev/urandom").pipe(cmd("true")).read()
```


## Work with pathlib.
If you have a `Path` object, you can use it anywhere you would use a
string.

```python
from duct import cmd
from pathlib import Path

myscript = Path("foo")
mydir = Path("bar")
cmd(myscript).dir(mydir).run()
```


## Reference

#### `cmd`

Create a command expression from a program name and optional arguments.
This doesn't require escaping any special characters or whitespace. If
your arguments are anything other than constant strings, this is
definitely what you want to use.

```python
x = "hi"
cmd("echo", x).run()
```

#### `run`

Execute the expression and return a `Result` object, which has fields
`stdout`, `stderr`, and `status`. By default, the child process shares
the stdin/stdout/stderr pipes of the parent, and no output is captured.
If the expression has a non-zero exit status, `run` will raise an
exception.

```python
result = cmd("echo", "foo").stdout_capture().run()
assert result.status == 0
assert result.stdout == b"foo\n"
assert result.stderr == b""
```

#### `read`

Execute the expression and capture its output, similar to backticks or
`$()` in bash. This is a convenience wrapper around `run` which sets
`stdout_capture`, decodes stdout as UTF-8, trims trailing newlines, and
returns it directly instead of returning a `Result`. Note that in Python
2 the return value is a *unicode* string.

```python
output = cmd("echo", u"日本語").read()
assert output == u"日本語"
```

#### `start`

Start the expression running in the background and immediately return a
`WaitHandle`. Calling `wait` on the handle waits for the expression to
finish running and then returns a `Result`, so `start` followed by
`wait` is equivalent to `run`.

```python
handle = cmd("echo", "foo").stdout_capture().start()
result = handle.wait()
assert result.status == 0
assert result.stdout == b"foo\n"
assert result.stderr == b""
```

#### `pipe`

Create a pipe expression, similar to `|` in bash. The argument becomes
the right side of the pipe, and it can be any duct expression. The
status of a pipe expression is equal to the right side's status if it's
nonzero, otherwise the left side's.

```python
output = cmd("echo", "dog").pipe(cmd("sed", "s/o/a/")).read()
assert output == "dag"
```

#### `input`

Redirects an expression's stdin to read from a string or bytes object.
Duct will spawn a writer thread at runtime.

```python
output = cmd("cat").input("stuff").read()
assert output == "stuff"
```

#### `stdin`, `stdin_file`, `stdin_null`

Redirects an expression's stdin to read from a file. The file can be a
string/bytes/pathlib path to open at runtime, or with `stdin_file` an
already open file or descriptor. `stdin_null` redirects stdin to
`/dev/null` on Unix and `nul` on Windows.

```python
cmd("cat").stdin("/etc/resolv.conf").run()
cmd("cat").stdin_null().run()
```

#### `stdout`, `stdout_file`, `stdout_null`, `stdout_capture`, `stdout_to_stderr`

Redirects an expression's stdout to write to a file. The file can by a
string/bytes/pathlib path to open at runtime, or with `stdout_file` an
already open file or descriptor. `stdout_null` redirects to `/dev/null`
on Unix or `nul` on Windows. `stdout_capture` redirects to a pipe whose
output bytes end up as `Result.stdout`. `stdout_to_stderr` replaces
stdout with a copy of the stderr pipe.

```python
from duct import cmd
from pathlib import Path

temp_dir = cmd("mktemp", "-d").read()
temp_file = Path(temp_dir) / "file.txt"
cmd("echo", "some stuff").stdout(temp_file).run()

result = cmd("echo", "more stuff").stdout_capture().run()
assert result.stdout == b"more stuff\n"
```

#### `stderr`, `stderr_file`, `stderr_null`, `stderr_capture`, `stderr_to_stdout`

Analogous to the `stdout` methods. `stderr_capture` redirects to a pipe
whose output bytes end up as `Result.stderr`.

```python
from duct import cmd

cmd("foo").stderr_null().run()

result = cmd("foo").stderr_capture().run()
print(result.stderr)
```

#### `dir`

Sets the working directory an expression will execute with. The default
is the working directory of the parent.

```python
output = cmd("pwd").dir("/").read()
assert output == "/"
```

#### `env`

Sets an environment variable for an expression, given a name and a
value.

```python
output = cmd("bash", "-c", "echo $FOO").env("FOO", "bar").read()
assert output == "bar"
```

#### `env_remove`

Unsets an environment variable for an expression, whether it's from the
parent environment, or from an exterior (but not interior) call to
`env`.

```python
os.environ["FOO"] = "bar"
output = cmd("bash", "-c", "echo $FOO").env_remove("FOO").read()
assert output == ""
```

#### `full_env`

Sets the entire environment for an expression, so that nothing is
inherited. This includes both the parent processes's environment, and
any calls to `env` in parent expressions.

```python
# BAR and BAZ are guaranteed to be undefined when this runs.
prog = cmd("bash", "-c", "echo $FOO$BAR$BAZ").full_env({"FOO": "1"})

# This env var would normally get inherited by the child, but full_env
# above will prevent it.
os.environ["BAR"] = "2"

# This env call also gets suppressed.
output = prog.env("BAZ", "3").read()
assert output == "1"
```

#### `unchecked`

Prevents a non-zero exit status from causing `run` or `read` to return
an error. The unchecked exit code will still be there on the `Result`
returned by `run`; its value doesn't change.

"Uncheckedness" sticks to an exit code as it bubbles up through
complicated expressions, but it doesn't "infect" other exit codes. So
for example, if only one sub-expression in a pipe has `unchecked`, then
errors returned by the other side will still be checked. That said, most
commonly you'll just call `unchecked` right before `run`, and it'll
apply to an entire expression. This sub-expression stuff doesn't usually
come up unless you have a big pipeline built out of lots of different
pieces.

```python
# Raises a StatusError!
cmd("false").run()

# Does not raise an error.
result = cmd("false").unchecked().run()
assert result.status == 0
```
