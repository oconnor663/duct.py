from collections import namedtuple
from contextlib import contextmanager
import io
import os
import shutil
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
    """Build a command :class:`Expression` from a program name and any number
    of arguments.

    This is the entry point to Duct. Everything else is methods on the
    :class:`Expression` returned by :func:`cmd`.
    """
    return Expression(CMD, None, (prog, args))


class Expression:
    """An expression object representing a command or a pipeline of commands.

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
        """Execute the expression and return an :class:`Output`, which includes
        the exit status and any captured output. Raise an exception if the
        status is non-zero.
        """
        return self.start().wait()

    def read(self):
        """Execute the expression and capture its output, similar to backticks
        or $() in the shell.

        This is a wrapper around reader() which reads to EOF, decodes UTF-8,
        trims newlines, and returns the resulting string.
        """
        stdout_bytes = self.reader().read()
        stdout_str = decode_with_universal_newlines(stdout_bytes)
        return stdout_str.rstrip('\n')

    def start(self):
        """Start executing the expression and return a :class:`Handle`.

        Calling :func:`start` followed by :func:`wait` is equivalent to
        :func:`run`.
        """
        with new_iocontext() as context:
            handle = start_expression(self, context)
            context.stdout_capture_context.start_thread_if_needed()
            context.stderr_capture_context.start_thread_if_needed()
            return handle

    def reader(self):
        """Start executing the expression with its stdout captured, and return
        a :class:`ReaderHandle` wrapping the capture pipe.

        Note that while :func:`start` uses background threads to do IO,
        :func:`reader` does not, and it's the caller's responsibility to read
        the child's output promptly. Otherwise the child's stdout pipe buffer
        can fill up, causing the child to block and potentially leading to
        performance issues or deadlocks.
        """
        with new_iocontext() as context:
            handle = start_expression(self.stdout_capture(), context)
            read_pipe = context.stdout_capture_context.get_read_pipe()
            context.stderr_capture_context.start_thread_if_needed()
            return ReaderHandle(handle, read_pipe)

    def pipe(self, right_side):
        r"""Combine two expressions to form a pipeline.
        """
        return Expression(PIPE, None, (self, right_side))

    def stdin_bytes(self, buf):
        r"""Redirect the standard input of the expression to a pipe, and write
        the supplied bytes to the pipe using a background thread.

        This also accepts a string, in which case it converts any "\n"
        characters to os.linesep and encodes the result as UTF-8.
        """
        return Expression(STDIN_BYTES, self, buf)

    def stdin_path(self, path):
        return Expression(STDIN_PATH, self, path)

    def stdin_file(self, file_):
        return Expression(STDIN_FILE, self, file_)

    def stdin_null(self):
        return Expression(STDIN_NULL, self)

    def stdout_path(self, path):
        return Expression(STDOUT_PATH, self, path)

    def stdout_file(self, file_):
        return Expression(STDOUT_FILE, self, file_)

    def stdout_null(self):
        return Expression(STDOUT_NULL, self)

    def stdout_capture(self):
        return Expression(STDOUT_CAPTURE, self)

    def stdout_to_stderr(self):
        return Expression(STDOUT_TO_STDERR, self)

    def stderr_path(self, path):
        return Expression(STDERR_PATH, self, path)

    def stderr_file(self, file_):
        return Expression(STDERR_FILE, self, file_)

    def stderr_null(self):
        return Expression(STDERR_NULL, self)

    def stderr_capture(self):
        return Expression(STDERR_CAPTURE, self)

    def stderr_to_stdout(self):
        return Expression(STDERR_TO_STDOUT, self)

    def stdout_stderr_swap(self):
        return Expression(STDOUT_STDERR_SWAP, self)

    def dir(self, path):
        return Expression(DIR, self, path)

    def env(self, name, val):
        return Expression(ENV, self, (name, val))

    def env_remove(self, name):
        return Expression(ENV_REMOVE, self, name)

    def full_env(self, env_dict):
        return Expression(FULL_ENV, self, env_dict)

    def unchecked(self):
        return Expression(UNCHECKED, self)

    def before_spawn(self, callback):
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
    """The return type of :func:`run` and :func:`wait`.
    """
    __slots__ = ()


class StatusError(subprocess.CalledProcessError):
    """The exception raised by default when a child exits with a non-zero exit
    status. See the :func:`unchecked` method for suppressing this.
    """
    def __init__(self, output, expression_str):
        self.output = output
        self._expression_str = expression_str

    def __str__(self):
        return 'Expression {0} returned non-zero exit status: {1}'.format(
            self._expression_str, self.output)


class Handle:
    """A handle.
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
        status, output = wait_on_status_and_output(self)
        if is_checked_error(status):
            raise StatusError(output, self._expression_str)
        return output

    def try_wait(self):
        status = wait_on_status(self, False)
        if status is None:
            return None
        else:
            return self.wait()

    def kill_and_wait(self):
        kill(self)
        wait_on_status_and_output(self)


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
        with write:
            # If the write blocks on a full pipe buffer (default 64 KB on
            # Linux), and then the program on the other end quits before
            # reading everything, the write will throw. Catch this error.
            try:
                shutil.copyfileobj(input_reader, write)
            except PIPE_CLOSED_ERROR:
                pass

    thread = ThreadWithReturn(write_thread)
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

        self._thread = ThreadWithReturn(read_fn)
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


# The standard Thread class doesn't give us any way to access the return value
# of the target function, or to see any exceptions that might've gotten thrown.
# This is a thin wrapper around Thread that enhances the join function to
# return values and reraise exceptions.
class ThreadWithReturn(threading.Thread):
    def __init__(self, target, args=(), kwargs=None, **thread_kwargs):
        threading.Thread.__init__(self, **thread_kwargs)
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

# This wrapper works around two major deadlock issues to do with pipes. The
# first is that, before Python 3.2 on POSIX systems, os.pipe() creates
# inheritable file descriptors, which leak to all child processes and prevent
# reads from reaching EOF. The workaround for this is to set close_fds=True on
# POSIX, which was not the default in those versions. See PEP 0446 for many
# details.

# The second issue arises on Windows, where we're not allowed to set
# close_fds=True while also setting stdin/stdout/stderr. Descriptors from
# os.pipe() on Windows have never been inheritable, so it would seem that we're
# safe. However, the Windows implementation of subprocess.Popen() creates
# temporary inheritable copies of its descriptors, and these can leak. The
# workaround for this is to protect Popen() with a global lock. See
# https://bugs.python.org/issue25565.


# This function also returns a SharedChild object, which wraps
# subprocess.Popen. That type works around another race condition to do with
# signaling children.
def safe_popen(*args, **kwargs):
    close_fds = (os.name != 'nt')
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
    if os.name == 'nt':
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
        self._status = None
        # The status lock is only held long enough to read or write the status,
        # or to make non-blocking calls like Popen.poll(). Threads making a
        # blocking call to os.waitid() release the status lock first. This
        # ensures that one thread can call try_wait() while another thread is
        # blocked on wait().
        self._status_lock = threading.Lock()
        self._wait_lock = threading.Lock()

    def wait(self):
        with self._wait_lock:
            # See if another thread already waited. If so, return the status we
            # got before. If not, immediately release the status lock, and move
            # on to call wait ourselves.
            with self._status_lock:
                if self._status is not None:
                    return self._status

            # No other thread has waited, we're holding the wait lock, and
            # we've released the status lock. It's now our job to wait. As
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

            # Finally, while still holding the wait lock, re-acquire the status
            # lock to reap the child and write the result. Since we know the
            # child has already exited, this won't block. Any other waiting
            # threads that were blocked on us will see our result.
            with self._status_lock:
                # If the child was already reaped above in the !HAS_WAITID
                # branch, this will just return the same status again.
                self._status = self._child.wait()
                return self._status

    def try_wait(self):
        with self._status_lock:
            if self._status is not None:
                return self._status

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
        # released the status lock here, because wait() will re-acquire it.
        if poll_result is not None:
            return self.wait()
        else:
            return None

    def kill(self):
        with self._status_lock:
            if self._status is None:
                self._child.kill()


class ReaderHandle(io.BufferedIOBase):
    """A stdout reader that automatically closes its read pipe and awaits child
    processes once EOF is reached.

    This inherits from :class:`io.BufferedIOBase`, and you can call
    :func:`read` and related methods on it. If the child exits with a non-zero
    status, and :func:`unchecked` was not used, the final :func:`read` will
    raise a :class:`StatusError`.

    When a :class:`ReaderHandle` is used as a context manager, context exit
    will automatically call :func:`close`.
    """
    def __init__(self, handle, read_pipe):
        self._handle = handle
        self._read_pipe = read_pipe

    def read(self, size=-1):
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
        """Close the read pipe and, if EOF has not already been read, call
        :func:`kill_and_wait` on the inner :class:`Handle`.
        """
        if self._read_pipe is not None:
            self._handle.kill_and_wait()  # Does not raise StatusError.
            self._read_pipe.close()
            self._read_pipe = None
