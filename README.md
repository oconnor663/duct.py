# duct.py [![Build Status](https://travis-ci.org/oconnor663/duct.py.svg?branch=master)](https://travis-ci.org/oconnor663/duct.py) [![Build status](https://ci.appveyor.com/api/projects/status/0ecgamtb43j8o8ig/branch/master?svg=true)](https://ci.appveyor.com/project/oconnor663/duct-py/branch/master) [![Coverage Status](https://coveralls.io/repos/oconnor663/duct.py/badge.svg?branch=master&service=github)](https://coveralls.io/github/oconnor663/duct.py?branch=master)

A Python library for shelling out.


## Easy things should be easy.

But always be explicit about what happens to output.

```python
from duct import cmd, sh

# Read the name of the current git branch.
current_branch = sh('git symbolic-ref --short HEAD').read()

# Log the current branch, with git taking over the terminal as usual.
cmd('git', 'log', current_branch).run()
```

That's exactly the same as the following in standard Python 3.5:

```python
from subprocess import run, PIPE

result = run('git symbolic-ref --short HEAD', shell=True, stdout=PIPE,
             universal_newlines=True)
current_branch = result.stdout.rstrip('\n')

run(['git', 'log', current_branch])
```


## Crazy things should be possible.

Sometimes you have to write ridiculous pipelines in bash:

```bash
(echo error >&2 && echo output) 2>&1 | grep stuff
```

The duct version is longer, but duct expressions are composable objects,
so we can build the whole command piece-by-piece:

```python
from duct import cmd, sh, STDOUT, STDERR

echoes = cmd('echo', 'error').stdout(STDERR).then('echo', 'output')
pipeline = echoes.stderr(STDOUT).pipe(sh('grep stuff'))
output = pipeline.read()  # This raises an exception! See below.
```


## Errors should never pass silently.

Because `grep` in the example above doesn't match any lines, it's going
to return an error code, and duct will raise an exception. To ignore the
error, you can use `unchecked`:

```python
result = cmd('false').unchecked().run()
print(result.status)  # 0
```

If you need to know the value of a non-zero exit status, you can catch
the exception it raises and inspect it like this.

```python
from duct import cmd, StatusError

try:
    cmd('false').run()
except StatusError as e:
    print(e.result.status)  # 1
```

Note that duct treats errors in a pipe like bash's `pipefail` option:
they count even when they happen on the left. This can be surprising in
cases where we usually ignore errors. In the following example, `cat`
returns an error because its stdout is closed:

```python
# Raises an exception, because cat returns an error.
cmd('cat').stdin('/dev/urandom').pipe(cmd('true')).read()
```


## Work with pathlib.
If you have a `Path` object, you can use it anywhere you would use a
string.

```python
from duct import cmd
from pathlib import Path

myscript = Path('foo')
mydir = Path('bar')
cmd(myscript).cwd(mydir).run()
```


## Reference

### Expression starting functions

#### `cmd`

Create a command expression from a program name and optional arguments.
This doesn't require escaping any special characters or whitespace. If
your arguments are anything other than constant strings, this is
definitely what you want to use.

#### `sh`

Create a command expression from a string of shell code, executed with
the `shell=True` flag in the `subprocess` module. This can spare you
from typing a lot of quote characters, or even whole pipelines, but
please don't use it with anything other than a constant string, because
shell escaping is tricky.

### Execution methods

#### `run`

Execute the expression and return a `Result` object, which has fields
`stdout`, `stderr`, and `status`. By default, the child process shares
the stdin/stdout/stderr pipes of the parent, and no output is captured.
If the expression has a non-zero exit status, `run` will raise an
exception.

#### `read`

Execute the expression and capture its output, similar to backticks or
`$()` in bash. This is a convenience wrapper around `run` which sets
`stdout(CAPTURE)`, decodes stdout to a string, trims trailing newlines,
and returns it directly instead of returning a `Result`.

### Pipe building methods

#### `pipe`

Create a pipe expression, similar to `|` in bash. The the argument is
the right side of the pipe, which can be any duct expression. The status
of a pipe expression is equal to the right side's status if it's
nonzero, otherwise the left side's.

#### `then`

Create a sequence expression, similar to `&&` in bash, and used like
`pipe` above. The left side runs, and then if its status is zero, the
right side runs. If you want to ignore errors on the left side, similar
to `;` in bash, use `unchecked` around the left expression.

### Redirections etc.

#### `input`

Redirects an expression's stdin to read from a string or bytes object.
Duct will spawn a writer thread at runtime.

#### `stdin`

Redirects an expression's stdin to read from a file. The file can be a
string/bytes/pathlib filepath to open at runtime, an already open file
or descriptor, or `DEVNULL`.

#### `stdout`

Redirects an expression's stdout to write to a file, similar to `stdin`
above. In addition to paths, files, and `DEVNULL`, you can pass `STDERR`
to join stdout with the stderr stream. You can also pass `CAPTURE`, in
which case duct will spawn a reader thread at runtime and capture stdout
bytes as `Result.stdout`.

#### `stderr`

Similar to `stdout`. You can pass `STDOUT` to join stderr with the
stdout stream. Output captured with `CAPTURE` is returned as
`Result.stderr`.

#### `cwd`

Sets the working directory an expression will execute with. The default
is the working directory of the parent.

#### `env`

Sets an environment variable for an expression, given a name and a
value.

#### `env_remove`

Unsets an environment variable for an expression, given a name. If the
variable wasn't defined, this is a no op.

#### `env_clear`

Clears the entire parent environment, including `env` values. Note that
environment variables like `SYSTEMROOT` on Windows might be required by
child processes, and it's the caller's responsibility to re-`env` these
variables as needed.

#### `unchecked`

Forces an expression to return `0` as its exit status. This can be used
on the left side of `then`, to make sure the right side always executes.
