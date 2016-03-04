# duct [![Build Status](https://travis-ci.org/oconnor663/duct.svg?branch=master)](https://travis-ci.org/oconnor663/duct) [![Build status](https://ci.appveyor.com/api/projects/status/i7kdylq9klgw993g/branch/master?svg=true)](https://ci.appveyor.com/project/oconnor663/duct/branch/master) [![Coverage Status](https://coveralls.io/repos/oconnor663/duct/badge.svg?branch=master&service=github)](https://coveralls.io/github/oconnor663/duct?branch=master)

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
so you don't have to write the whole thing in one go. Note that `then`
and `pipe` accept the same arguments as `cmd`, but they can also accept
other duct expressions, like `sh` in this example:

```python
from duct import cmd, sh, STDOUT, STDERR

echoes = cmd('echo', 'error', stdout=STDERR).then('echo', 'output')
pipeline = echoes.subshell(stderr=STDOUT).pipe(sh('grep stuff'))
output = pipeline.read()  # This raises an exception! See below.
```


## Errors should never pass silently.

Because `grep` in the example above doesn't match any lines, it's going
to return an error code, and duct will raise an exception. To prevent
the exception, use `check=False`. If you pass that to `run`, the
expression's exit status will be the `returncode` attribute on the
result, just like `subprocess.run` in Python 3.5. If you pass it to part
of an expression, like `cmd`, it will force just that part to return
`0`, though other parts could still return errors.

```python
result = cmd('false').run(check=False)
print(result.returncode)  # 1

result = cmd('false', check=False).run()
print(result.returncode)  # 0
```

Note that duct treats errors in a pipe like bash's `pipefail` option:
they count even when they happen on the left. This can be surprising in
cases where we usually ignore errors. In the following example, `cat`
returns an error because its stdout is closed:

```python
# Raises an exception, because cat returns an error.
cmd('cat', stdin='/dev/urandom').pipe('true').read()
```


## Work with pathlib.
If you have `Path` objects, you can use them anywhere you would use a
string.

```python
from duct import cmd
from pathlib import Path

myscript = Path('foo')
mydir = Path('bar')
cmd(myscript).run(cwd=mydir)
```


## Reference

Every duct function takes the same keyword arguments, [see
below](#keyword-arguments).

### Top level functions

<strong><tt>cmd</tt></strong>(<em>program, \*program_args, \*\*kwargs</em>)

Create a command expression from a program name and optional arguments.
This doesn't require escaping any special characters or whitespace. If
your arguments are anything other than constant strings, this is
definitely what you want to use.

<strong><tt>sh</tt></strong>(<em>shell_command, \*\*kwargs</em>)

Create a command expression from a string of shell code, executed with
the `shell=True` flag in the `subprocess` module. This can spare you
from typing a lot of quote characters, or even whole pipelines, but
please don't use it with anything other than a constant command string,
because shell escaping is tricky.

### Expression methods

<strong><tt>run</tt></strong>(<em>\*\*kwargs</em>)

Execute the expression and return a `Result` object, which has fields
`stdout`, `stderr`, and `returncode`. By default, the child process
shares the stdin/stdout/stderr pipes of the parent, and no output is
captured. If the expression has a non-zero returncode, `run` will raise
an exception. Use `check=False` to allow non-zero returncodes.

<strong><tt>read</tt></strong>(<em>\*\*kwargs</em>)

Execute the expression and capture its output, similar to backticks or
`$()` in bash. This is a wrapper around `run`, which sets `stdout=PIPE`,
`decode=True`, and `sh_trim=True` and returns the `stdout` field of the
result.

<strong><tt>pipe</tt></strong>(<em>\*command_or_expression, \*\*kwargs</em>)

Create a pipe expression, similar to `|` in bash. The the right is any
duct expression. The returncode of a pipe expression is equal to the
right side's returncode if it's nonzero, otherwise the left side's.

<strong><tt>then</tt></strong>(<em>\*command_or_expression, \*\*kwargs</em>)

Create a sequence expression, similar to `&&` in bash, with syntax like
`pipe` above. The left side runs, and then if its returncode is zero,
the right side runs. If you want to ignore errors on the left side,
similar to `;` in bash, use `check=False` inside the left expression.

<strong><tt>subshell</tt></strong>(<em>\*\*kwargs</em>)

Apply redirections or other keywords to an already-formed expression,
similar to `()` in bash. You don't usually need this; instead you can
pass these arguments to `run` or to individual commands. `subshell` is
useful when you're composing expressions that were created in some other
part of your program, or when you're translating absurd bash pipelines.

### Keyword arguments

Except where noted, all of these are valid as arguments both to the
`run` and `read` methods (the "run level"), and to individual commands
(the "expression level"). Arguments given at the expression level will
generally override arguments given to containing expressions or at the
run level.

<strong><tt>input</tt></strong>

A string or bytes object to write directly to standard input.

<strong><tt>stdin</tt></strong>

A file to use in place of the default standard input. It can be a
string/bytes/pathlib filepath to open, an already open file or
descriptor, or `DEVNULL`. Setting this and `input` at the same time is
an error.

<strong><tt>stdout</tt></strong>

A file to use in place of the default standard output. It can be a
string/bytes/pathlib filepath to open, an already open file or
descriptor, or `DEVNULL`. Also accepts `STDERR` to join with the stderr
pipe. (Setting `stdout=STDOUT` is a no-op. Setting `stdout=STDERR` and
`stderr=STDOUT` at the same time swaps them.) Also accepts `PIPE`, which
causes output to be captured and stored as the `stdout` field of the
`Result` object returned by `run`. `PIPE` only work at the run level.

<strong><tt>stderr</tt></strong>

Similar to `stdout`. Output captured with `PIPE` is stored as the
`stderr` field of the `Result` object returned by `run`.

<strong><tt>cwd</tt></strong>

The working directory of the child process. The default is the working
directory of the parent.

<strong><tt>env</tt></strong>

A map of environment variables set for the child process. Note that this
is *in addition* to what's in `os.environ`, unlike the "env" parameter
from the `subprocess` module. Using `env` at both the run level and the
expression level is cumulative. If you set the same variable in both
places, the expression level wins.

<strong><tt>full_env</tt></strong>

The complete map of environment variables for the child process, which
will not be merged with `os.environ`. This is what the `subprocess`
module calls "env". Setting `full_env` at the expression level wipes out
any other variables set with `env` or `full_env` at the run level.

<strong><tt>check</tt></strong>

Defaults to `True`. If `False` at the expression level, that expression
always returns exit status `0`. If `False` at the run level, `run` will
return results with a nonzero `returncode`, instead of raising an
exception.

<strong><tt>sh_trim</tt></strong>

Defaults to `False` in `run` and `True` in `read`. If `True`, trailing
newlines get stripped from any output captured with `PIPE` and
`decode=True`. This is the same behavior as backticks or `$()` in bash.
Output captured without `decode` is never trimmed. Only valid at the run
level.
