# -*- coding: UTF-8 -*-
r"""\
Duct is a library for running child processes. Duct makes it easy to build
pipelines and redirect IO like a shell. At the same time, Duct helps you write
correct, portable code: whitespace is never significant, errors from child
processes get reported by default, and a variety of `gotchas, bugs, and
platform inconsistencies
<https://github.com/oconnor663/duct.py/blob/master/gotchas.md>`_ are handled
for you the Right Way™.

- `GitHub repo <https://github.com/oconnor663/duct.rs>`_
- `PyPI package <https://pypi.python.org/pypi/duct>`_
- `the same library, in Rust <https://github.com/oconnor663/duct.rs>`_

Examples
--------

Run a command without capturing any output. Here "hi" is printed directly to
the terminal:

>>> from duct import cmd
>>> cmd("echo", "hi").run() # doctest: +SKIP
hi
Output(status=0, stdout=None, stderr=None)

Capture the standard output of a command. Here "hi" is returned as a string:

>>> cmd("echo", "hi").read()
'hi'

Capture the standard output of a pipeline:

>>> cmd("echo", "hi").pipe(cmd("sed", "s/i/o/")).read()
'ho'

Merge standard error into standard output and read both incrementally:

>>> big_cmd = cmd("bash", "-c", "echo out && echo err 1>&2")
>>> reader = big_cmd.stderr_to_stdout().reader()
>>> with reader:
...     reader.readlines()
[b'out\n', b'err\n']

Children that exit with a non-zero status raise an exception by default:

>>> cmd("false").run()
Traceback (most recent call last):
...
duct.StatusError: Expression cmd('false') returned non-zero exit status: Output(status=1, stdout=None, stderr=None)
>>> cmd("false").unchecked().run()
Output(status=1, stdout=None, stderr=None)
"""  # noqa: E501

from collections import namedtuple
from contextlib import contextmanager
import io
import os
import shutil
import signal
import subprocess
import threading

try:
    from pathlib import PurePath
except ImportError:
    # a dummy class that nothing will ever be an instance of
    class PurePath:
        pass


try:
    # not defined in Python 2
    PIPE_CLOSED_ERROR = BrokenPipeError
except NameError:
    PIPE_CLOSED_ERROR = IOError

HAS_WAITID = "waitid" in dir(os)

# Expression and handle types.
# TODO: Replace this with enum when we no longer support Python 2.
CMD = 0
PIPE = 1
STDIN_BYTES = 2
STDIN_PATH = 3
STDIN_FILE = 4
STDIN_NULL = 5
STDOUT_PATH = 6
STDOUT_FILE = 7
STDOUT_NULL = 8
STDOUT_CAPTURE = 9
STDOUT_TO_STDERR = 10
STDERR_PATH = 11
STDERR_FILE = 12
STDERR_NULL = 13
STDERR_CAPTURE = 14
STDERR_TO_STDOUT = 15
STDOUT_STDERR_SWAP = 16
DIR = 17
ENV = 18
ENV_REMOVE = 19
FULL_ENV = 20
UNCHECKED = 21
BEFORE_SPAWN = 22

NAMES = {
    CMD: "cmd",
    PIPE: "pipe",
    STDIN_BYTES: "stdin_bytes",
    STDIN_PATH: "stdin_path",
    STDIN_FILE: "stdin_file",
    STDIN_NULL: "stdin_null",
    STDOUT_PATH: "stdout_path",
    STDOUT_FILE: "stdout_file",
    STDOUT_NULL: "stdout_null",
    STDOUT_CAPTURE: "stdout_capture",
    STDOUT_TO_STDERR: "stdout_to_stderr",
    STDERR_PATH: "stderr_path",
    STDERR_FILE: "stderr_file",
    STDERR_NULL: "stderr_null",
    STDERR_CAPTURE: "stderr_capture",
    STDERR_TO_STDOUT: "stderr_to_stdout",
    STDOUT_STDERR_SWAP: "stdout_stderr_swap",
    DIR: "dir",
    ENV: "env",
    ENV_REMOVE: "env_remove",
    FULL_ENV: "full_env",
    UNCHECKED: "unchecked",
    BEFORE_SPAWN: "before_spawn",
}


def cmd(prog, *args):
    r"""Build a command :class:`Expression` from a program name and any number
    of arguments.

    This is the sole entry point to Duct. All the types below are built with
    methods on the :class:`Expression` returned by this function.

    >>> cmd("echo", "hi").read()
    'hi'
    """
    return Expression(CMD, None, (prog, args))


class Expression:
    r"""An expression object representing a command or a pipeline of commands.

    Build command expressions with the :func:`cmd` function. Build pipelines
    with the :func:`pipe` method. Methods like :func:`stdout_path` and
    :func:`env` also return new expressions representing the modified execution
    environment. Execute expressions with :func:`run`, :func:`read`,
    :func:`start`, or :func:`reader`.
    """
    def __init__(self, _type, inner, payload=None):
        self._type = _type
        self._inner = inner
        self._payload = payload

    def __repr__(self):
        return repr_expression(self)

    def run(self):
        r"""Execute the expression and return an :class:`Output`, which includes
        the exit status and any captured output. Raise an exception if the
        status is non-zero.

        >>> cmd("true").run()
        Output(status=0, stdout=None, stderr=None)
        """
        return self.start().wait()

    def read(self):
        r"""Execute the expression and capture its output, similar to backticks
        or $() in the shell.

        This is a wrapper around reader() which reads to EOF, decodes UTF-8,
        trims newlines, and returns the resulting string.

        >>> cmd("echo", "hi").read()
        'hi'
        """
        stdout_bytes = self.reader().read()
        stdout_str = decode_with_universal_newlines(stdout_bytes)
        return stdout_str.rstrip('\n')

    def start(self):
        r"""Start executing the expression and return a :class:`Handle`.

        Calling :func:`start` followed by :func:`Handle.wait` is equivalent to
        :func:`run`.

        >>> handle = cmd("echo", "hi").stdout_capture().start()
        >>> # Do some other stuff.
        >>> handle.wait()
        Output(status=0, stdout=b'hi\n', stderr=None)

        Note that leaking a :class:`Handle` without calling :func:`Handle.wait`
        will turn the children into zombie processes. In a long-running
        program, that could be serious resource leak.
        """
        with new_iocontext() as context:
            handle = start_expression(self, context)
            context.stdout_capture_context.start_thread_if_needed()
            context.stderr_capture_context.start_thread_if_needed()
            return handle

    def reader(self):
        r"""Start executing the expression with its stdout captured, and return
        a :class:`ReaderHandle` wrapping the capture pipe.

        Note that while :func:`start` uses background threads to do IO,
        :func:`reader` does not, and it's the caller's responsibility to read
        the child's output promptly. Otherwise the child's stdout pipe buffer
        can fill up, causing the child to block and potentially leading to
        performance issues or deadlocks.

        >>> reader = cmd("echo", "hi").reader()
        >>> with reader:
        ...     reader.read()
        b'hi\n'
        """
        with new_iocontext() as context:
            handle = start_expression(self.stdout_capture(), context)
            read_pipe = context.stdout_capture_context.get_read_pipe()
            context.stderr_capture_context.start_thread_if_needed()
            return ReaderHandle(handle, read_pipe)

    def pipe(self, right_side):
        r"""Combine two expressions to form a pipeline.

        >>> cmd("echo", "hi").pipe(cmd("sed", "s/i/o/")).read()
        'ho'

        During execution, if one side of the pipe returns a non-zero exit
        status, that becomes the status of the whole pipe, similar to Bash's
        ``pipefail`` option. If both sides return non-zero, and one of them is
        :func:`unchecked`, then the checked side wins. Otherwise the right side
        wins.

        During spawning, if the left side of the pipe spawns successfully, but
        the right side fails to spawn, the left side will be killed and
        awaited. That's necessary to return the spawn errors immediately,
        without leaking the left side as a zombie.
        """
        return Expression(PIPE, None, (self, right_side))

    def stdin_bytes(self, buf):
        r"""Redirect the standard input of the expression to a pipe, and write
        the supplied bytes to the pipe using a background thread.

        This also accepts a string, in which case it converts any ``\n``
        characters to ``os.linesep`` and encodes the result as UTF-8.

        >>> cmd("cat").stdin_bytes(b"foo").read()
        'foo'
        """
        return Expression(STDIN_BYTES, self, buf)

    def stdin_path(self, path):
        r"""Redirect the standard input of the expression to a file opened from
        the supplied filepath.

        This works with strings, bytes, and pathlib :class:`Path` objects.

        >>> cmd("head", "-c10").stdin_path("/dev/zero").read()
        '\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
        """
        return Expression(STDIN_PATH, self, path)

    def stdin_file(self, file_):
        r"""Redirect the standard input of the expression to the supplied file.
        This works with any file-like object accepted by :class:`Popen`,
        including raw file descriptors.

        >>> f = open("/dev/zero")
        >>> cmd("head", "-c10").stdin_file(f).read()
        '\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
        """
        return Expression(STDIN_FILE, self, file_)

    def stdin_null(self):
        r"""Redirect the standard input of the expression to ``/dev/null``.

        >>> cmd("cat").stdin_null().read()
        ''
        """
        return Expression(STDIN_NULL, self)

    def stdout_path(self, path):
        r"""Redirect the standard output of the expression to a file opened
        from the supplied filepath.

        This works with strings, bytes, and pathlib :class:`Path` objects.

        >>> cmd("echo", "hi").stdout_path("/tmp/outfile").run()
        Output(status=0, stdout=None, stderr=None)
        >>> open("/tmp/outfile").read()
        'hi\n'
        """
        return Expression(STDOUT_PATH, self, path)

    def stdout_file(self, file_):
        r"""Redirect the standard output of the expression to the supplied file.
        This works with any file-like object accepted by :class:`Popen`,
        including raw file descriptors.

        >>> f = open("/dev/null", "w")
        >>> cmd("echo", "hi").stdout_file(f).run()
        Output(status=0, stdout=None, stderr=None)
        """
        return Expression(STDOUT_FILE, self, file_)

    def stdout_null(self):
        r"""Redirect the standard output of the expression to ``/dev/null``.

        >>> cmd("echo", "hi").stdout_null().run()
        Output(status=0, stdout=None, stderr=None)
        """
        return Expression(STDOUT_NULL, self)

    def stdout_capture(self):
        r"""Capture the standard output of the expression. The captured bytes
        become the ``stdout`` field of the returned :class:`Output`.

        >>> cmd("echo", "hi").stdout_capture().run()
        Output(status=0, stdout=b'hi\n', stderr=None)
        """
        return Expression(STDOUT_CAPTURE, self)

    def stdout_to_stderr(self):
        r"""Merge the standard output of the expression with its stderr.

        >>> bash_cmd = cmd("bash", "-c", "echo out && echo err 1>&2")
        >>> bash_cmd.stdout_to_stderr().stdout_capture().stderr_capture().run()
        Output(status=0, stdout=b'', stderr=b'out\nerr\n')
        """
        return Expression(STDOUT_TO_STDERR, self)

    def stderr_path(self, path):
        r"""Redirect the standard error of the expression to a file opened from
        the supplied filepath.

        This works with strings, bytes, and pathlib :class:`Path` objects.

        >>> cmd("bash", "-c", "echo hi 1>&2").stderr_path("/tmp/outfile").run()
        Output(status=0, stdout=None, stderr=None)
        >>> open("/tmp/outfile").read()
        'hi\n'
        """
        return Expression(STDERR_PATH, self, path)

    def stderr_file(self, file_):
        r"""Redirect the standard error of the expression to the supplied file.
        This works with any file-like object accepted by :class:`Popen`,
        including raw file descriptors.

        >>> f = open("/dev/null", "w")
        >>> cmd("bash", "-c", "echo hi 1>&2").stderr_file(f).run()
        Output(status=0, stdout=None, stderr=None)
        """
        return Expression(STDERR_FILE, self, file_)

    def stderr_null(self):
        r"""Redirect the standard error of the expression to ``/dev/null``.

        >>> cmd("bash", "-c", "echo hi 1>&2").stderr_null().run()
        Output(status=0, stdout=None, stderr=None)
        """
        return Expression(STDERR_NULL, self)

    def stderr_capture(self):
        r"""Capture the standard error of the expression. The captured bytes
        become the ``stderr`` field of the returned :class:`Output`.

        >>> cmd("bash", "-c", "echo hi 1>&2").stderr_capture().run()
        Output(status=0, stdout=None, stderr=b'hi\n')
        """
        return Expression(STDERR_CAPTURE, self)

    def stderr_to_stdout(self):
        r"""Merge the standard error of the expression with its stdout.

        >>> bash_cmd = cmd("bash", "-c", "echo out && echo err 1>&2")
        >>> bash_cmd.stderr_to_stdout().stdout_capture().stderr_capture().run()
        Output(status=0, stdout=b'out\nerr\n', stderr=b'')
        """
        return Expression(STDERR_TO_STDOUT, self)

    def stdout_stderr_swap(self):
        r"""Swap the standard output and standard error of the expression.

        >>> bash_cmd = cmd("bash", "-c", "echo out && echo err 1>&2")
        >>> swapped_cmd = bash_cmd.stdout_stderr_swap()
        >>> swapped_cmd.stdout_capture().stderr_capture().run()
        Output(status=0, stdout=b'err\n', stderr=b'out\n')
        """
        return Expression(STDOUT_STDERR_SWAP, self)

    def dir(self, path):
        r"""Set the working directory for the expression.

        >>> cmd("pwd").dir("/").read()
        '/'

        Note that :func:`dir` does *not* affect the meaning of relative exe
        paths.  For example in the expression ``cmd("./foo.sh").dir("bar")``,
        the script ``./foo.sh`` will execute, *not* the script
        ``./bar/foo.sh``. However, it usually *does* affect how the child
        process interprets relative paths in command arguments.
        """
        return Expression(DIR, self, path)

    def env(self, name, val):
        r"""Set an environment variable in the expression's environment.

        >>> cmd("bash", "-c", "echo $FOO").env("FOO", "bar").read()
        'bar'
        """
        return Expression(ENV, self, (name, val))

    def env_remove(self, name):
        r"""Unset an environment variable in the expression's environment.

        >>> os.environ["FOO"] = "bar"
        >>> cmd("bash", "-c", "echo $FOO").env_remove("FOO").read()
        ''

        Note that all of Duct's ``env`` functions follow OS rules for
        environment variable case sensitivity. That means that
        ``env_remove("foo")`` will unset ``FOO`` on Windows (where ``foo`` and
        ``FOO`` are equivalent) but not on Unix (where they are separate
        variables). Portable programs should restrict themselves to uppercase
        environment variable names for that reason.
        """
        return Expression(ENV_REMOVE, self, name)

    def full_env(self, env_dict):
        r"""Set the entire environment for the expression, from a dictionary of
        name-value pairs.

        >>> os.environ["FOO"] = "bar"
        >>> os.environ["BAZ"] = "bing"
        >>> cmd("bash", "-c", "echo $FOO$BAZ").full_env({"FOO": "xyz"}).read()
        'xyz'

        Note that some environment variables are required for normal program
        execution (like SystemRoot on Windows), so copying the parent's
        environment is usually preferable to starting with an empty one.
        """
        return Expression(FULL_ENV, self, env_dict)

    def unchecked(self):
        r"""Prevent a non-zero exit status from raising a :class:`StatusError`.
        The unchecked exit code will still be there on the :class:`Output`
        returned by :func:`run`; its value doesn't change.

        >>> cmd("false").run()
        Traceback (most recent call last):
        ...
        duct.StatusError: Expression cmd('false') returned non-zero exit status: Output(status=1, stdout=None, stderr=None)
        >>> cmd("false").unchecked().run()
        Output(status=1, stdout=None, stderr=None)

        "Uncheckedness" sticks to an exit code as it propagates up from part of
        a pipeline, but it doesn't "infect" other exit codes. So for example,
        if only one sub-expression in a pipe is :func:`unchecked`, then errors
        returned by the other side will still be checked.

        >>> cmd("false").pipe(cmd("true")).unchecked().run()
        Output(status=1, stdout=None, stderr=None)
        >>> cmd("false").unchecked().pipe(cmd("true")).run()
        Output(status=1, stdout=None, stderr=None)
        >>> cmd("false").pipe(cmd("true").unchecked()).run()
        Traceback (most recent call last):
        ...
        duct.StatusError: Expression cmd('false').pipe(cmd('true').unchecked()) returned non-zero exit status: Output(status=1, stdout=None, stderr=None)
        """  # noqa: E501
        return Expression(UNCHECKED, self)

    def before_spawn(self, callback):
        r"""
        Add a callback for modifying the arguments to :func:`Popen` right
        before it's called. The callback will be passed a command list (the
        program followed by its arguments) and a keyword arguments dictionary,
        and it may modify either. The callback's return value is ignored.

        The callback is called for each command in its sub-expression, and each
        time the expression is executed. That call happens after other features
        like :func:`stdout` and :func:`env` have been applied, so any changes
        made by the callback take priority. More than one callback can be
        added, in which case the innermost is executed last. For example, if
        one call to :func:`before_spawn` is applied to an entire :func:`pipe`
        expression, and another call is applied to just one command within the
        pipeline, the callback for the entire pipeline will be called first
        over the command where both hooks apply.

        This is intended for rare and tricky cases, like callers who want to
        change the group ID of their child processes, or who want to run code
        in :func:`Popen.preexec_fn`. Most callers shouldn't need to use it.

        >>> def add_sneaky_arg(command, kwargs):
        ...     command.append("sneaky!")
        >>> cmd("echo", "being").before_spawn(add_sneaky_arg).read()
        'being sneaky!'
        """
        return Expression(BEFORE_SPAWN, self, callback)


def start_expression(expression, context):
    handle_inner = None
    handle_payload_cell = [None]

    if expression._type == CMD:
        prog, args = expression._payload
        handle_payload_cell[0] = start_cmd(context, prog, args)
    elif expression._type == PIPE:
        left_expr, right_expr = expression._payload
        handle_payload_cell[0] = start_pipe(context, left_expr, right_expr)
    else:
        # IO redirect expressions
        with modify_context(expression, context,
                            handle_payload_cell) as modified_context:
            handle_inner = start_expression(expression._inner,
                                            modified_context)

    return Handle(expression._type, handle_inner, handle_payload_cell[0],
                  str(expression), context.stdout_capture_context,
                  context.stderr_capture_context)


def start_cmd(context, prog, args):
    prog_str = stringify_with_dot_if_path(prog)
    maybe_absolute_prog = maybe_canonicalize_exe_path(prog_str, context)
    args_strs = [stringify_if_path(arg) for arg in args]
    command = [maybe_absolute_prog] + args_strs
    kwargs = {
        "cwd": context.dir,
        "env": context.env,
        "stdin": context.stdin,
        "stdout": context.stdout,
        "stderr": context.stderr,
    }
    # The innermost hooks are pushed last, and we execute them last.
    for hook in context.before_spawn_hooks:
        hook(command, kwargs)
    return safe_popen(command, **kwargs)


def start_pipe(context, left_expr, right_expr):
    read_pipe, write_pipe = open_pipe()
    with read_pipe:
        with write_pipe:
            # Start the left side first. If this fails for some reason,
            # just let the failure propagate.
            left_context = context._replace(stdout=write_pipe)
            left_handle = start_expression(left_expr, left_context)

        # Now the left side is started. If the right side fails to start,
        # we can't let the left side turn into a zombie. We have to await
        # it, and that means we have to kill it.
        right_context = context._replace(stdin=read_pipe)
        try:
            right_handle = start_expression(right_expr, right_context)
        except Exception:
            kill(left_handle)
            # This wait helper function doesn't throw on non-zero statuses or
            # join capture threads.
            wait_on_status(left_handle, True)
            raise

    return (left_handle, right_handle)


@contextmanager
def modify_context(expression, context, payload_cell):
    arg = expression._payload

    if expression._type == STDIN_BYTES:
        if is_unicode(arg):
            buf = encode_with_universal_newlines(arg)
        elif is_bytes(arg):
            buf = arg
        else:
            raise TypeError("Not a valid stdin_bytes parameter: " + repr(arg))
        input_reader = io.BytesIO(buf)
        with start_input_thread(input_reader, payload_cell) as read_pipe:
            yield context._replace(stdin=read_pipe)

    elif expression._type == STDIN_PATH:
        with open_path(arg, "rb") as f:
            yield context._replace(stdin=f)

    elif expression._type == STDIN_FILE:
        yield context._replace(stdin=arg)

    elif expression._type == STDIN_NULL:
        with open_devnull("rb") as f:
            yield context._replace(stdin=f)

    elif expression._type == STDOUT_PATH:
        with open_path(arg, "wb") as f:
            yield context._replace(stdout=f)

    elif expression._type == STDOUT_FILE:
        yield context._replace(stdout=arg)

    elif expression._type == STDOUT_NULL:
        with open_devnull("wb") as f:
            yield context._replace(stdout=f)

    elif expression._type == STDOUT_CAPTURE:
        yield context._replace(
            stdout=context.stdout_capture_context.get_write_pipe())

    elif expression._type == STDOUT_TO_STDERR:
        yield context._replace(stdout=context.stderr)

    elif expression._type == STDERR_PATH:
        with open_path(arg, "wb") as f:
            yield context._replace(stderr=f)

    elif expression._type == STDERR_FILE:
        yield context._replace(stderr=arg)

    elif expression._type == STDERR_NULL:
        with open_devnull("wb") as f:
            yield context._replace(stderr=f)

    elif expression._type == STDERR_CAPTURE:
        yield context._replace(
            stderr=context.stderr_capture_context.get_write_pipe())

    elif expression._type == STDERR_TO_STDOUT:
        yield context._replace(stderr=context.stdout)

    elif expression._type == STDOUT_STDERR_SWAP:
        yield context._replace(stdout=context.stderr, stderr=context.stdout)

    elif expression._type == DIR:
        yield context._replace(dir=stringify_if_path(arg))

    elif expression._type == ENV:
        # Don't modify the environment dictionary in place. That would affect
        # all references to it. Make a copy instead.
        name, val = arg
        new_env = context.env.copy()
        # Windows needs special handling of env var names.
        new_env[convert_env_var_name(name)] = stringify_if_path(val)
        yield context._replace(env=new_env)

    elif expression._type == ENV_REMOVE:
        # As above, don't modify the dictionary in place.
        new_env = context.env.copy()
        # Windows needs special handling of env var names.
        new_env.pop(convert_env_var_name(arg), None)
        yield context._replace(env=new_env)

    elif expression._type == FULL_ENV:
        # Windows needs special handling of env var names.
        new_env = dict((convert_env_var_name(k), v) for (k, v) in arg.items())
        yield context._replace(env=new_env)

    elif expression._type == UNCHECKED:
        # Unchecked only affects what happens during wait.
        yield context

    elif expression._type == BEFORE_SPAWN:
        # As with env, don't modify the list in place. Make a copy.
        before_spawn_hooks = context.before_spawn_hooks + [arg]
        yield context._replace(before_spawn_hooks=before_spawn_hooks)

    else:
        raise NotImplementedError  # pragma: no cover


class Output(namedtuple('Output', ['status', 'stdout', 'stderr'])):
    r"""The return type of :func:`Expression.run` and :func:`Handle.wait`. It
    carries the pubic fields ``status``, ``stdout``, and ``stderr``. If
    :func:`Expression.stdout_capture` and :func:`Expression:stderr_capture`
    aren't used, ``stdout`` and ``stderr`` respectively will be ``None``.

    >>> cmd("bash", "-c", "echo hi 1>&2").stderr_capture().run()
    Output(status=0, stdout=None, stderr=b'hi\n')
    """
    __slots__ = ()


class StatusError(subprocess.CalledProcessError):
    r"""The exception raised by default when a child exits with a non-zero exit
    status. See :func:`Expression.unchecked` for suppressing this. If the
    exception is caught, the ``output`` field contains the :class:`Output`.

    >>> from duct import StatusError
    >>> try:
    ...     cmd("bash", "-c", "echo hi 1>&2 && false").stderr_capture().run()
    ... except StatusError as e:
    ...     e.output
    Output(status=1, stdout=None, stderr=b'hi\n')
    """
    def __init__(self, output, expression_str):
        self.output = output
        self._expression_str = expression_str

    def __str__(self):
        return 'Expression {0} returned non-zero exit status: {1}'.format(
            self._expression_str, self.output)


class Handle:
    r"""A handle representing one or more running child processes, returned by
    the :func:`Expression.start` method.

    Note that leaking a :class:`Handle` without calling :func:`wait` will turn
    the children into zombie processes. In a long-running program, that could
    be serious resource leak.
    """
    def __init__(self, _type, inner, payload, expression_str,
                 stdout_capture_context, stderr_capture_context):
        self._type = _type
        self._inner = inner
        self._payload = payload
        self._expression_str = expression_str
        self._stdout_capture_context = stdout_capture_context
        self._stderr_capture_context = stderr_capture_context

    def wait(self):
        r"""Wait for the child process(es) to finish and return an
        :class:`Output` containing the exit status and any captured output.
        This frees the OS resources associated with the child.

        >>> handle = cmd("true").start()
        >>> handle.wait()
        Output(status=0, stdout=None, stderr=None)
        """
        status, output = wait_on_status_and_output(self)
        if is_checked_error(status):
            raise StatusError(output, self._expression_str)
        return output

    def try_wait(self):
        r"""Check whether the child process(es) have finished, and if so return
        an :class:`Output` containing the exit status and any captured output.
        If the child has exited, this frees the OS resources associated with
        it.

        >>> handle = cmd("sleep", "1000").unchecked().start()
        >>> assert handle.try_wait() is None
        >>> handle.kill()
        >>> handle.try_wait()
        Output(status=-9, stdout=None, stderr=None)
        """
        status = wait_on_status(self, False)
        if status is None:
            return None
        else:
            return self.wait()

    def kill(self):
        r"""Send a kill signal to the child process(es). This is equivalent to
        :func:`Popen.kill`, which uses ``SIGKILL`` on Unix. After sending the
        signal, wait for the child to finish and free the OS resources
        associated with it. If the child has already been waited on, this has
        no effect.

        This function does not return an :class:`Output`, and it does not raise
        :class:`StatusError`. However, subsequent calls to :func:`wait` or
        :func:`try_wait` are likely to raise :class:`StatusError` if you didn't
        use :func:`Expression.unchecked`.

        >>> handle = cmd("sleep", "1000").start()
        >>> handle.kill()
        """
        kill(self)
        # Note that this *must not* call wait_on_status_and_output. There might
        # be un-signaled grandchild processes holding the output pipe, and we
        # can't expect them to exit promptly. We only want to reap our
        # immediate zombie children here. See gotchas.md for an extensive
        # discussion of why we can't do better.
        wait_on_status(self, True)

    def pids(self):
        r"""Return the PIDs of all the running child processes. The order of
        the PIDs in the returned list is the same as the pipeline order, from
        left to right.
        """
        return pids(self)


# This function handle waiting and collecting output, but does not raise status
# errors for non-zero exit statuses.
def wait_on_status_and_output(handle):
    status = wait_on_status(handle, True)
    stdout = handle._stdout_capture_context.join_thread_if_needed()
    stderr = handle._stderr_capture_context.join_thread_if_needed()
    output = Output(status.code, stdout, stderr)
    return (status, output)


def wait_on_status(handle, blocking):
    if handle._type == CMD:
        shared_child = handle._payload
        return wait_child(shared_child, blocking)
    elif handle._type == PIPE:
        left, right = handle._payload
        return wait_pipe(left, right, blocking)

    status = wait_on_status(handle._inner, blocking)
    if blocking:
        assert status is not None

    if handle._type == STDIN_BYTES:
        io_thread = handle._payload
        if status is not None:
            io_thread.join()
    elif handle._type == UNCHECKED:
        if status is not None:
            status = status._replace(checked=False)

    return status


def wait_child(shared_child, blocking):
    if blocking:
        status = shared_child.wait()
    else:
        status = shared_child.try_wait()
    if not blocking and status is None:
        return None
    assert status is not None
    return ExecStatus(code=status, checked=True)


def wait_pipe(left, right, blocking):
    left_status = wait_on_status(left, blocking)
    right_status = wait_on_status(right, blocking)
    if not blocking and (left_status is None or right_status is None):
        return None
    assert left_status is not None and right_status is not None
    if is_checked_error(right_status):
        return right_status
    elif is_checked_error(left_status):
        return left_status
    elif right_status.code != 0:
        return right_status
    else:
        return left_status


def kill(handle):
    if handle._type == CMD:
        shared_child = handle._payload
        shared_child.kill()
    elif handle._type == PIPE:
        left, right = handle._payload
        kill(left)
        kill(right)
    else:
        kill(handle._inner)


def pids(handle):
    if handle._type == CMD:
        shared_child = handle._payload
        return [shared_child.pid()]
    elif handle._type == PIPE:
        left, right = handle._payload
        return pids(left) + pids(right)
    else:
        return pids(handle._inner)


def repr_expression(expression):
    if expression._type == CMD:
        prog, args = expression._payload
        args_str = repr(prog)
        for arg in args:
            args_str += ", " + repr(arg)
        return "cmd({})".format(args_str)
    elif expression._type == PIPE:
        left, right = expression._payload
        return "{}.pipe({})".format(repr_expression(left),
                                    repr_expression(right))
    else:
        name = NAMES[expression._type]
        inner = repr_expression(expression._inner)
        arg = ""
        if expression._payload is not None:
            if type(expression._payload) is tuple:
                arg = ", ".join(repr(x) for x in expression._payload)
            else:
                arg = repr(expression._payload)
        return "{}.{}({})".format(inner, name, arg)


# The IOContext represents the child process environment at any given point in
# the execution of an expression. We read the working directory and the entire
# environment when we create a new execution context. Methods like .env(),
# .dir(), and .pipe() will create new modified contexts and pass those to their
# children. The IOContext does *not* own any of the file descriptors it's
# holding -- it's the caller's responsibility to close those.
IOContext = namedtuple("IOContext", [
    "stdin",
    "stdout",
    "stderr",
    "dir",
    "env",
    "stdout_capture_context",
    "stderr_capture_context",
    "before_spawn_hooks",
])


@contextmanager
def new_iocontext():
    # Hardcode the standard file descriptors. We can't rely on None here,
    # becase stdout/stderr swapping needs to work.
    context = IOContext(
        stdin=0,
        stdout=1,
        stderr=2,
        dir=os.getcwd(),
        # Pretend this dictionary is immutable please.
        env=os.environ.copy(),
        stdout_capture_context=OutputCaptureContext(),
        stderr_capture_context=OutputCaptureContext(),
        before_spawn_hooks=[],
    )
    try:
        yield context
    finally:
        context.stdout_capture_context.close_write_pipe_if_needed()
        context.stderr_capture_context.close_write_pipe_if_needed()


ExecStatus = namedtuple("ExecStatus", ["code", "checked"])


def is_checked_error(exec_status):
    return exec_status.code != 0 and exec_status.checked


@contextmanager
def open_devnull(mode):
    # We open devnull ourselves because Python 2 doesn't support DEVNULL.
    with open(os.devnull, mode) as f:
        yield f


def is_bytes(val):
    # Note that bytes is the same as str in Python 2.
    return isinstance(val, (bytes, bytearray))


def is_unicode(val):
    unicode_type = type(u"")
    return isinstance(val, unicode_type)


@contextmanager
def open_path(path_or_string, mode):
    with open(stringify_if_path(path_or_string), mode) as f:
        yield f


@contextmanager
def start_input_thread(input_reader, writer_thread_cell):
    read, write = open_pipe()

    def write_thread():
        # If the write blocks on a full pipe buffer (default 64 KB on Linux),
        # and then the program on the other end quits before reading
        # everything, the write will throw. Catch this error.
        #
        # Note that on macOS, *both* write *and* close can raise a
        # BrokenPipeError. So we put the try on the outside.
        try:
            with write:
                shutil.copyfileobj(input_reader, write)
        except PIPE_CLOSED_ERROR:
            pass

    thread = DaemonicThread(write_thread)
    writer_thread_cell[0] = thread
    thread.start()
    with read:
        yield read


# The stdout_capture() and stderr_capture() pipes are shared by all
# sub-expressions, but we don't want to open them if nothing is going to be
# captured. Also we don't want to spawn background reader threads when nothing
# is captured, or when the calling thread will be reading. This type handles
# the bookkeeping for all of that.
class OutputCaptureContext:
    def __init__(self):
        self._read_pipe = None
        self._write_pipe = None
        self._thread = None

    def get_write_pipe(self):
        if self._write_pipe is None:
            self._read_pipe, self._write_pipe = open_pipe()
        return self._write_pipe

    def get_read_pipe(self):
        assert self._read_pipe is not None
        return self._read_pipe

    def close_write_pipe_if_needed(self):
        if self._write_pipe is not None:
            self._write_pipe.close()

    def start_thread_if_needed(self):
        if self._read_pipe is None:
            return

        def read_fn():
            with self._read_pipe:
                return self._read_pipe.read()

        self._thread = DaemonicThread(read_fn)
        self._thread.start()

    def join_thread_if_needed(self):
        if self._thread is not None:
            return self._thread.join()
        else:
            return None


def stringify_if_path(x):
    if isinstance(x, PurePath):
        return str(x)
    return x


# Pathlib never renders a leading './' in front of a local path. That's an
# issue because on POSIX subprocess.py (like bash) won't execute scripts in the
# current directory without it. In the same vein, we also don't want
# Path('echo') to match '/usr/bin/echo' from the $PATH. To work around both
# issues, we explicitly join a leading dot to any relative pathlib path.
def stringify_with_dot_if_path(x):
    if isinstance(x, PurePath):
        # Note that join does nothing if the path is absolute.
        return os.path.join('.', str(x))
    return x


# A thread that sets the daemon flag to true, so that it doesn't block process
# exit. This also includes several other conveniences:
# - It takes a target function argument in its constructor, so that you don't
#   have to subclass it every time you use it.
# - The return value from join() is whatever the target function returned.
# - join() re-raises any exceptions from the target function.
class DaemonicThread(threading.Thread):
    def __init__(self, target, args=(), kwargs=None, **thread_kwargs):
        threading.Thread.__init__(self, **thread_kwargs)
        self.daemon = True
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self._return = None
        self._exception = None

    def run(self):
        try:
            self._return = self._target(*self._args, **self._kwargs)
        except Exception as e:
            self._exception = e

    def join(self):
        threading.Thread.join(self)
        if self._exception is not None:
            raise self._exception
        return self._return


def open_pipe():
    read_fd, write_fd = os.pipe()
    read_mode, write_mode = ('rb', 'wb')
    return os.fdopen(read_fd, read_mode), os.fdopen(write_fd, write_mode)


# There's a tricky interaction between exe paths and `dir`. Exe paths can be
# relative, and so we have to ask: Is an exe path interpreted relative to the
# parent's cwd, or the child's? The answer is that it's platform dependent! >.<
# (Windows uses the parent's cwd, but because of the fork-chdir-exec pattern,
# Unix usually uses the child's.)
#
# We want to use the parent's cwd consistently, because that saves the caller
# from having to worry about whether `dir` will have side effects, and because
# it's easy for the caller to use path.join if they want to. That means that
# when `dir` is in use, we need to detect exe names that are relative paths,
# and absolutify them. We want to do that as little as possible though, both
# because canonicalization can fail, and because we prefer to let the caller
# control the child's argv[0].
#
# We never want to absolutify a name like "emacs", because that's probably a
# program in the PATH rather than a local file. So we look for slashes in the
# name to determine what's a filepath and what isn't. Note that anything given
# as a Path will always have a slash by the time we get here, because
# stringify_with_dot_if_path prepends a ./ to them when they're relative. This
# leaves the case where Windows users might pass a local file like "foo.bat" as
# a string, which we can't distinguish from a global program name. However,
# because the Windows has the preferred "relative to parent's cwd" behavior
# already, this case actually works without our help. (The thing Windows users
# have to watch out for instead is local files shadowing global program names,
# which I don't think we can or should prevent.)
def maybe_canonicalize_exe_path(exe_name, iocontext):
    has_sep = (os.path.sep in exe_name
               or (os.path.altsep is not None and os.path.altsep in exe_name))

    if has_sep and iocontext.dir is not None and not os.path.isabs(exe_name):
        return os.path.realpath(exe_name)
    else:
        return exe_name


popen_lock = threading.Lock()


def is_windows():
    return os.name == "nt"


# This wrapper works around two major deadlock issues to do with pipes. The
# first is that, before Python 3.2 on POSIX systems, os.pipe() creates
# inheritable file descriptors, which leak to all child processes and prevent
# reads from reaching EOF. The workaround for this is to set close_fds=True on
# POSIX, which was not the default in those versions. See PEP 0446 for many
# details.
#
# TODO: Revisit this workaround when we drop Python 2 support.
#
# The second issue arises on Windows, where we're not allowed to set
# close_fds=True while also setting stdin/stdout/stderr. Descriptors from
# os.pipe() on Windows have never been inheritable, so it would seem that we're
# safe. However, the Windows implementation of subprocess.Popen() creates
# temporary inheritable copies of its descriptors, and these can leak. The
# workaround for this is to protect Popen() with a global lock. See
# https://bugs.python.org/issue25565.
#
# This function also returns a SharedChild object, which wraps
# subprocess.Popen. That type works around another race condition to do with
# signaling children.
def safe_popen(*args, **kwargs):
    close_fds = not is_windows()
    with popen_lock:
        return SharedChild(*args, close_fds=close_fds, **kwargs)


# We could let our pipes do this for us, by opening them in universal newlines
# mode, but it's a bit cleaner to do it ourselves. That saves us from passing
# around the mode all over the place, and from having decoding exceptions
# thrown on reader threads.
def decode_with_universal_newlines(b):
    return b.decode('utf8').replace('\r\n', '\n').replace('\r', '\n')


def encode_with_universal_newlines(s):
    return s.replace('\n', os.linesep).encode('utf8')


# Environment variables are case-insensitive on Windows. To deal with that,
# Python on Windows converts all the keys in os.environ to uppercase
# internally. That's mostly transparent when we deal with os.environ directly,
# but when we call os.environ.copy(), we get a regular dictionary with all the
# keys uppercased. We need to do a similar conversion, or else additions and
# removals in that copy won't interact properly with the inherited parent
# environment.
def convert_env_var_name(var):
    if is_windows():
        return var.upper()
    return var


# The wait() and kill() methods on the standard library Popen class have a race
# condition on Unix. Normally kill() checks to see whether a process has
# already been awaited before sending a signal, so that if the PID has been
# reused by an unrelated process in the meantime it won't accidentally signal
# that unrelated process. However, if kill() and wait() are called from
# different threads, it's possible for wait() to free the PID *after* kill()
# has seen that the child is still running. If the kill() thread pauses at
# exactly that moment, long enough for the OS to reuse the PID, kill() could
# kill the wrong process. This is unlikely under ordinary circumstances, but
# more likely if the system is under heavy load and the PID space is almost
# exhausted.
#
# The workaround for this race condition on Unix is to use:
#
#     os.waitid(os.P_PID, child_pid, os.WEXITED | os.WNOWAIT)
#
# That call waits on the child to exit, but *doesn't* free its PID for reuse.
# Then we set an internal flag that's synchronized with kill(), before finally
# calling wait() to reap the child.
#
# Note that Windows doesn't have this problem, because child handles (unlike
# raw PIDs) have to be explicitly closed.
class SharedChild:
    def __init__(self, *args, **kwargs):
        self._child = subprocess.Popen(*args, **kwargs)
        # The child lock is only held for non-blocking calls. Threads making a
        # blocking call to os.waitid() release the child lock first. This
        # ensures that one thread can call try_wait() while another thread is
        # blocked on wait().
        self._child_lock = threading.Lock()
        self._wait_lock = threading.Lock()

    def wait(self):
        with self._wait_lock:
            # See if another thread already waited. If so, return the status we
            # got before. If not, immediately release the child lock, and move
            # on to call wait ourselves.
            with self._child_lock:
                if self._child.returncode is not None:
                    return self._child.returncode

            # No other thread has waited, we're holding the wait lock, and
            # we've released the child lock. It's now our job to wait. As
            # documented above, if os.waitid is defined, use that function to
            # await the child without reaping it. Otherwise we do an ordinary
            # Popen.wait and accept the race condition on some platforms.
            if HAS_WAITID:
                os.waitid(os.P_PID, self._child.pid, os.WEXITED | os.WNOWAIT)
            else:
                # Python does synchronize this internally, so it won't race
                # with other calls to wait() or poll(). Unfortunately it still
                # races with kill(), which is what all of this is about.
                self._child.wait()

            # Finally, while still holding the wait lock, re-acquire the child
            # lock to reap the child and write the result. Since we know the
            # child has already exited, this won't block. Any other waiting
            # threads that were blocked on us will see our result.
            with self._child_lock:
                # If the child was already reaped above in the !HAS_WAITID
                # branch, this second wait will be a no-op with a cached
                # returncode.
                return self._child.wait()

    def try_wait(self):
        with self._child_lock:
            if self._child.returncode is not None:
                return self._child.returncode

            # The child hasn't been waited on yet, so we need to do a
            # non-blocking check to see if it's still running. The Popen type
            # provides the poll() method for this, but that might reap the
            # child and free its PID, which would make this a race with
            # concurrent callers of the blocking wait() method above, who might
            # be about to call os.waitid on that PID. When os.waitid is
            # available, use that again here, with the WNOHANG flag. Otherwise
            # just use poll() and rely on Python's internal synchronization.
            if HAS_WAITID:
                poll_result = os.waitid(os.P_PID, self._child.pid,
                                        os.WEXITED | os.WNOWAIT | os.WNOHANG)
            else:
                poll_result = self._child.poll()

        # If either of the poll approaches above returned non-None, do a full
        # wait to reap the child, which will not block. Note that we've
        # released the child lock here, because wait() will re-acquire it.
        if poll_result is not None:
            return self.wait()
        else:
            return None

    def kill(self):
        with self._child_lock:
            if self._child.returncode is None:
                # Previously we just used Popen.kill here. However, as of
                # Python 3.9, Popen.send_signal (which is called by Popen.kill)
                # calls Popen.poll first, as a best-effort check for the same
                # PID race that this class is designed around. That means that
                # if the child has already exited, Popen.kill will reap it. Now
                # that we check Popen.returncode throughout this class (as of
                # the same commit that adds this comment), we'll see the
                # non-None exit status there as a side effect if reaping has
                # happened. That *might* mean we could still call Popen.kill
                # here safely. However, there's also the question of how
                # Popen.poll's call to os.waitpid would interact with our own
                # blocking call to os.waitid from another thread. The worry is
                # that the waitpid call might take effect first, causing waitid
                # to return a "no child found" error. I can confirm that
                # happens on Linux when both calls are blocking. Here though,
                # the waitpid call is non-blocking, which *might* mean it can't
                # happen first, but that's going to depend on the OS. We could
                # assume that it can happen and try to catch the error from
                # waitid, but that codepath would be impossible to test. So
                # what we actually do here is reimplement the documented
                # behavior of Popen.kill: os.kill(pid, SIGKILL) on Unix, and
                # Popen.terminate on Windows.
                if is_windows():
                    self._child.terminate()
                else:
                    os.kill(self._child.pid, signal.SIGKILL)

    def pid(self):
        return self._child.pid


class ReaderHandle(io.IOBase):
    r"""A stdout reader that automatically closes its read pipe and awaits
    child processes once EOF is reached.

    This inherits from :class:`io.IOBase`, and you can call :func:`read` and
    related methods like :func:`readlines` on it. When :class:`ReaderHandle` is
    used as a context manager with the ``with`` keyword, context exit will
    automatically call :func:`close`.

    Note that if you don't read to EOF, and you don't call :func:`close` or use
    a ``with`` statement, then the child will become a zombie. Using a ``with``
    statement is recommended for exception safety.

    If one thread is blocked on a call to :func:`read`, then calling
    :func:`kill` from another thread is an effective way to unblock the reader.
    However, note that killed child processes return a non-zero exit status,
    which turns into an exception for the reader by default, unless you use
    :func:`Expression.unchecked`.
    """
    def __init__(self, handle, read_pipe):
        self._handle = handle
        self._read_pipe = read_pipe

    def read(self, size=-1):
        r"""Read bytes from the child's standard output. Because
        :class:`ReaderHandle` inherits from :class:`io.IOBase`, related methods
        like :func:`readlines` are also available.

        >>> reader = cmd("printf", r"a\nb\nc\n").reader()
        >>> with reader:
        ...     reader.read(2)
        ...     reader.readlines()
        b'a\n'
        [b'b\n', b'c\n']

        If :func:`read` reaches EOF and awaits the child, and the child exits
        with a non-zero status, and :func:`Expression.unchecked` was not used,
        :func:`read` will raise a :class:`StatusError`.

        >>> with cmd("false").reader() as reader:
        ...     reader.read()
        Traceback (most recent call last):
        ...
        duct.StatusError: Expression cmd('false').stdout_capture() returned non-zero exit status: Output(status=1, stdout=None, stderr=None)
        """  # noqa: E501
        if self._read_pipe is None:
            self._handle.wait()  # May raise a StatusError.
            return b""
        is_zero_size = size == 0
        is_positive_size = type(size) is int and size > 0
        is_read_to_end = not is_zero_size and not is_positive_size
        ret = self._read_pipe.read(size)
        if is_read_to_end or (is_positive_size and ret == b""):
            self._read_pipe.close()
            self._read_pipe = None
            self._handle.wait()  # May raise a StatusError.
        return ret

    def close(self):
        r"""Close the read pipe and call :func:`kill` on the inner
        :class:`Handle`.

        :class:`ReaderHandle` is a context manager, and if you use it with the
        `with` keyword, context exit will automatically call :func:`close`.
        Using a ``with`` statement is recommended, for exception safety.

        >>> reader = cmd("echo", "hi").reader()
        >>> reader.close()
        """
        if self._read_pipe is not None:
            self._handle.kill()  # Does not raise StatusError.
            self._read_pipe.close()
            self._read_pipe = None

    def try_wait(self):
        r"""Check whether the child process(es) have finished, and if so return
        an :class:`Output` containing the exit status and any captured output.
        This is equivalent to :func:`Handle.try_wait`.

        Note that the ``stdout`` field of the returned :class:`Output` will
        always be ``None``, because the :class:`ReaderHandle` itself owns the
        child's stdout pipe.

        >>> input_bytes = bytes([42]) * 1000000
        >>> reader = cmd("cat").stdin_bytes(input_bytes).reader()
        >>> with reader:
        ...     assert reader.try_wait() is None
        ...     output_bytes = reader.read()
        ...     assert reader.try_wait() is not None
        ...     assert input_bytes == output_bytes
        """
        return self._handle.try_wait()

    def kill(self):
        r"""Call :func:`kill` on the inner :class:`Handle`.

        This function does not raise :class:`StatusError`. However, subsequent
        calls to :func:`read` are likely to raise :class:`StatusError` if you
        didn't use :func:`Expression.unchecked`.

        >>> reader = cmd("bash", "-c", "echo hi && sleep 1000000").unchecked().reader()
        >>> with reader:
        ...     reader.read(3)
        ...     reader.kill()
        ...     reader.read()
        b'hi\n'
        b''
        """  # noqa: E501
        self._handle.kill()

    def pids(self):
        r"""Return the PIDs of all the running child processes. The order of
        the PIDs in the returned list is the same as the pipeline order, from
        left to right.
        """
        return self._handle.pids()
