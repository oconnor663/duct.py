# duct [![Build Status](https://travis-ci.org/oconnor663/duct.svg?branch=master)](https://travis-ci.org/oconnor663/duct) [![Build status](https://ci.appveyor.com/api/projects/status/i7kdylq9klgw993g/branch/master?svg=true)](https://ci.appveyor.com/project/oconnor663/duct/branch/master) [![Coverage Status](https://coveralls.io/repos/oconnor663/duct/badge.svg?branch=master&service=github)](https://coveralls.io/github/oconnor663/duct?branch=master)

A Python library for shelling out.


## Easy things should be easy.

But always be explicit about what happens to output.

```python
from duct import cmd, sh

# Read the name of the current branch.
current_branch = sh('git symbolic-ref --short HEAD').read()

# Log the current branch, with git taking over the terminal as usual.
cmd('git', 'log', current_branch).run()
```

That's equivalent to the following (Python 3.5):

```python
from subprocess import run, PIPE

result = run('git symbolic-ref --short HEAD', shell=True, stdout=PIPE,
             universal_newlines=True)
current_branch = result.stdout.rstrip('\n')

run(['git', 'log', current_branch])
```


## Crazy things should be possible.

Bash:

```bash
(echo error >&2 && echo output) 2>&1 | grep stuff
```

Duct:

```python
from duct import cmd, sh, STDOUT, STDERR

echoes = cmd('echo', 'error', stdout=STDERR).then('echo', 'output')
pipeline = echoes.subshell(stderr=STDOUT).pipe(sh('grep stuff'))
pipeline.run()
```

The duct version is longer, but the commands are composable objects, so
you don't have to do the whole thing in one line. Note that `then` and
`pipe` accept the same arguments as `cmd`, but they can also accept any
other duct expression, like `sh` in this example.


## Errors should never pass silently.

Because `grep` returns an error, this will raise an exception:

```python
sh('grep foo').run(input="bar")
```

If you want to do that without an exception, you have to use
`check=True` either with command (which forces it to return `0`) or to
the `run` method (which preserves the returncode but suppresses the
exception).

Note that duct treats errors in pipes like bash's `pipefail` option:
they count even when they happen on the left. This can be confusing in
cases where we're used to errors being hidden. For example in the
following command, `cat` returns an error when its stdout closes:

```python
from duct import cmd, BYTES
cmd('cat', stdin='/dev/urandom').pipe('head', '-c', '10').read(stdout=BYTES)
```
