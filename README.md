# duct.py [![Actions Status](https://github.com/oconnor663/duct.py/workflows/tests/badge.svg)](https://github.com/oconnor663/duct.py/actions) [![Coverage Status](https://coveralls.io/repos/oconnor663/duct.py/badge.svg?branch=master&service=github)](https://coveralls.io/github/oconnor663/duct.py?branch=master) [![PyPI version](https://badge.fury.io/py/duct.svg)](https://pypi.python.org/pypi/duct) [![Documentation Status](https://readthedocs.org/projects/ductpy/badge/?version=latest)](https://ductpy.readthedocs.io/en/latest/?badge=latest)

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

- v0.6.3
  - Added `Handle.pids` and `ReaderHandle.pids`.
- v0.6.2
  - Added `ReaderHandle.try_wait`.
- v0.6.1
  - Added `ReaderHandle.kill`.
  - Kill methods no longer wait on IO threads to complete. This avoids
    blocking on unkilled grandchildren.
- v0.6.0
  - The `kill` method now reaps killed child processes before returning.
  - Removed the `sh` function.
  - Removed the `then` method.
  - Added `Handle.kill`.
  - Added `ReaderHandle` and `Expression.reader`.
  - Added `Expression.stdout_stderr_swap`.
  - Added `Expression.before_spawn`.
  - Renamed `stdin`/`stdout`/`stderr` to
    `stdin_path`/`stdout_path`/`stderr_path`.
  - Renamed `input` to `stdin_bytes`.
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

Related projects
================

-   [Brish](https://github.com/NightMachinary/brish) allows you to use persistent (or not) zsh sessions from Python. Brish uses Python's metaprogramming APIs to achieve near first-party interoperability between the two languages. 

-   [pysh](https://github.com/sharkdp/pysh) uses comments in bash scripts to switch the interpreter to Python, allowing variable reuse between the two.

-   [plumbum](https://github.com/tomerfiliba/plumbum) is a small yet feature-rich library for shell script-like programs in Python. It attempts to mimic the shell syntax (\"shell combinators\") where it makes sense, while keeping it all Pythonic and cross-platform.

-   [xonsh](https://github.com/xonsh/xonsh) is a superset of Python 3.5+ with additional shell primitives.

-   [daudin](https://github.com/terrycojones/daudin) [tries](https://github.com/terrycojones/daudin#how-commands-are-interpreted) to eval your code as Python, falling back to the shell if that fails. It does not currently reuse a shell session, thus incurring large overhead. I [think](https://github.com/terrycojones/daudin/issues/11) it can use Brish to solve this, but someone needs to contribute the support.

-   `python -c` can also be powerful, especially if you write yourself a helper library in Python and some wrappers in your shell dotfiles. An example:

    ``` {.example}
    alias x='noglob calc-raw'
    calc-raw () {
        python3 -c "from math import *; print($*)"
    }
    ```

-   [Z shell kernel for Jupyter Notebook](https://github.com/danylo-dubinin/zsh-jupyter-kernel) allows you to do all sorts of stuff if you spend the time implementing your usecase; See [emacs-jupyter](https://github.com/nnicandro/emacs-jupyter#org-mode-source-blocks) to get a taste of what\'s possible. [Jupyter Kernel Gateway](https://github.com/jupyter/kernel_gateway) also sounds promising, but I haven\'t tried it out yet. Beware the completion support in this kernel though. It uses a pre-alpha proof of concept [thingy](https://github.com/Valodim/zsh-capture-completion) that was very buggy when I tried it.

-   Finally, if you\'re feeling adventurous, try Rust\'s [rust_cmd_lib](https://github.com/rust-shell-script/rust_cmd_lib). It\'s quite beautiful.
