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
pipeline.run()  # This raises an exception! See below.
```


## Errors should never pass silently.

Because `grep` in the example above doesn't match any lines, it's going
to return an error. In duct, that means `run` is going to raise an
exception. To prevent that, use `check=False`. If you pass that argument
to `run`, the command's exit status will be the `returncode` attribute
on the result, just like `subprocess.run` in Python 3.5. If you pass it
to `cmd` or to another part of a duct expression, it will force that
part's exit status to be `0`, though other parts could still return
errors.

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
