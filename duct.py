from collections import namedtuple
from contextlib import contextmanager
import io
import os
import subprocess
import threading

try:
    from pathlib import PurePath
except ImportError:
    # a dummy class that nothing will ever be an instance of
    class PurePath:
        pass

# same as in the subprocess module
STDOUT = -2
DEVNULL = -3
# not defined in subprocess (value may change)
STDERR = -4
CAPTURE = -5


def cmd(prog, *args):
    return Cmd(prog, args)


def sh(shell_str):
    return Sh(shell_str)


class Expression(object):
    'Abstract base class for all expression types.'

    def run(self):
        '''Execute the expression and return a Result, which includes the exit
        status and any captured output. Raise an exception if the status is
        non-zero.'''
        with spawn_output_reader() as (stdout_capture, stdout_thread):
            with spawn_output_reader() as (stderr_capture, stderr_thread):
                context = starter_iocontext(stdout_capture, stderr_capture)
                status = self._exec(context)
        stdout_bytes = stdout_thread.join()
        stderr_bytes = stderr_thread.join()
        result = Result(status, stdout_bytes, stderr_bytes)
        if status != 0:
            raise StatusError(result, self)
        return result

    def read(self):
        '''Execute the expression and capture its output, similar to backticks
        or $() in the shell. This is a wrapper around run() which captures
        stdout, decodes it, trims it, and returns it directly.'''
        result = self.stdout(CAPTURE).run()
        stdout_str = decode_with_universal_newlines(result.stdout)
        return stdout_str.rstrip('\n')

    def pipe(self, right_side):
        return Pipe(self, right_side)

    def then(self, right_side):
        return Then(self, right_side)

    def input(self, buf):
        return Input(self, buf)

    def stdin(self, source):
        return Stdin(self, source)

    def stdout(self, sink):
        return Stdout(self, sink)

    def stderr(self, sink):
        return Stderr(self, sink)

    def cwd(self, path):
        return Cwd(self, path)

    def env(self, name, val):
        return Env(self, name, val)

    def env_remove(self, name):
        return EnvRemove(self, name)

    def env_clear(self):
        return EnvClear(self)

    def unchecked(self):
        return Unchecked(self)

    # Implemented by subclasses.

    def _exec(self, context):
        raise NotImplementedError  # pragma: no cover

    def __repr__(self):
        raise NotImplementedError  # pragma: no cover


Result = namedtuple('Result', ['status', 'stdout', 'stderr'])


class StatusError(subprocess.CalledProcessError):
    def __init__(self, result, expression):
        self.result = result
        self.expression = expression

    def __str__(self):
        return 'Expression {0} returned non-zero exit status: {1}'.format(
            self.expression, self.result)


class Cmd(Expression):
    def __init__(self, prog, args):
        '''The prog and args will be passed directly to subprocess.call(),
        which determines the types allowed here (strings and bytes). In
        addition, we also explicitly support pathlib Paths, by converting them
        to strings.'''
        prog_str = stringify_with_dot_if_path(prog)
        args_strs = tuple(stringify_if_path(arg) for arg in args)
        self._argv = (prog_str,) + args_strs

    def _exec(self, context):
        proc = safe_popen(
            self._argv, cwd=context.cwd, env=context.env, stdin=context.stdin,
            stdout=context.stdout, stderr=context.stderr)
        return proc.wait()

    def __repr__(self):
        return 'cmd({0})'.format(', '.join(repr(arg) for arg in self._argv))


class Sh(Expression):
    def __init__(self, shell_cmd):
        # The command could be a Path. This is potentially useful on Windows
        # where you have to run things like .py files in shell mode.
        self._shell_cmd = stringify_with_dot_if_path(shell_cmd)

    def _exec(self, context):
        proc = safe_popen(
            self._shell_cmd, shell=True, cwd=context.cwd, env=context.env,
            stdin=context.stdin, stdout=context.stdout, stderr=context.stderr)
        return proc.wait()

    def __repr__(self):
        return "sh({0})".format(repr(self._shell_cmd))


class Then(Expression):
    def __init__(self, left, right):
        self._left = left
        self._right = right

    def _exec(self, context):
        # Execute the first command.
        left_status = self._left._exec(context)
        # If it returns non-zero short-circuit.
        if left_status != 0:
            return left_status
        # Otherwise execute the second command.
        right_status = self._right._exec(context)
        return right_status

    def __repr__(self):
        return "{0}.then({1})".format(repr(self._left), repr(self._right))


# Pipe uses another thread to run the left side of the pipe in parallel with
# the right. This is required because both the left and the right might be
# compound expressions, where a second command might need to be started after
# the first finishes, so someone has to be waiting on both sides at the same
# time. There are Unix-specific ways to wait on multiple processes at once, but
# those can conflict with other listeners that might by in the same process,
# and they won't work on Windows anyway.
class Pipe(Expression):
    def __init__(self, left, right):
        self._left = left
        self._right = right

    def _exec(self, context):
        # Open a read/write pipe. The write end gets passed to the left as
        # stdout, and the read end gets passed to the right as stdin. Either
        # side could be a compound expression (like A.then(B)), so we have to
        # wait until each command is completely finished before we can close
        # its end of the pipe. Closing the write end allows the right side to
        # receive EOF, and closing the read end allows the left side to receive
        # SIGPIPE.
        read_pipe, write_pipe = open_pipe()
        right_context = context._replace(stdin=read_pipe)
        left_context = context._replace(stdout=write_pipe)

        def do_left():
            with write_pipe:
                return self._left._exec(left_context)
        left_thread = ThreadWithReturn(target=do_left)
        left_thread.start()

        with read_pipe:
            right_status = self._right._exec(right_context)
        left_status = left_thread.join()

        # Return the rightmost error, if any. Note that cwd and env changes
        # never propagate out of the pipe. This is the same behavior as bash.
        if right_status != 0:
            return right_status
        else:
            return left_status

    def __repr__(self):
        return "{0}.pipe({1})".format(repr(self._left), repr(self._right))


class Unchecked(Expression):
    def __init__(self, inner_expression):
        self._inner = inner_expression

    def _exec(self, context):
        self._inner._exec(context)
        return 0

    def __repr__(self):
        return "{0}.unchecked()".format(repr(self._inner))


class IORedirectExpression(Expression):
    def __init__(self, inner_expression, method_name, method_args):
        self._inner = inner_expression
        self._method_name = method_name
        self._method_args = ", ".join(ioarg_repr(arg) for arg in method_args)

    def _exec(self, context):
        with self._update_context(context) as updated_context:
            return self._inner._exec(updated_context)

    def __repr__(self):
        return "{0}.{1}({2})".format(
            repr(self._inner), self._method_name, self._method_args)

    # Implemented by subclasses.

    def _update_context(self, context):
        raise NotImplementedError  # pragma: no cover


class Input(IORedirectExpression):
    def __init__(self, inner, arg):
        super(Input, self).__init__(inner, "input", [arg])
        # If the argument is a string, convert it to bytes.
        # TODO: Might be cheaper to open the pipe in text mode.
        if is_unicode(arg):
            self._buf = encode_with_universal_newlines(arg)
        elif is_bytes(arg):
            self._buf = arg
        else:
            raise TypeError("Not a valid input parameter: " + ioarg_repr(arg))

    @contextmanager
    def _update_context(self, context):
        with spawn_input_writer(self._buf) as read_pipe:
            yield context._replace(stdin=read_pipe)


class Stdin(IORedirectExpression):
    def __init__(self, inner, source):
        super(Stdin, self).__init__(inner, "stdin", [source])
        self._source = source

    @contextmanager
    def _update_context(self, context):
        with child_stdin_pipe(self._source) as read_pipe:
            yield context._replace(stdin=read_pipe)


class Stdout(IORedirectExpression):
    def __init__(self, inner, sink):
        super(Stdout, self).__init__(inner, "stdout", [sink])
        self._sink = sink

    @contextmanager
    def _update_context(self, context):
        with child_output_pipe(context.stdout,
                               context.stderr,
                               context.stdout_capture,
                               self._sink) as write_pipe:
            yield context._replace(stdout=write_pipe)


class Stderr(IORedirectExpression):
    def __init__(self, inner, sink):
        super(Stderr, self).__init__(inner, "stderr", [sink])
        self._sink = sink

    @contextmanager
    def _update_context(self, context):
        with child_output_pipe(context.stdout,
                               context.stderr,
                               context.stderr_capture,
                               self._sink) as write_pipe:
            yield context._replace(stderr=write_pipe)


class Cwd(IORedirectExpression):
    def __init__(self, inner, path):
        super(Cwd, self).__init__(inner, "cwd", [path])
        self._path = stringify_if_path(path)

    @contextmanager
    def _update_context(self, context):
        yield context._replace(cwd=self._path)


class Env(IORedirectExpression):
    def __init__(self, inner, name, val):
        super(Env, self).__init__(inner, "env", [name, val])
        self._name = name
        self._val = stringify_if_path(val)

    @contextmanager
    def _update_context(self, context):
        # Pretend the IOContext is totally immutable. Copy its environment
        # dictionary instead of modifying it in place.
        new_env = context.env.copy()
        new_env[self._name] = self._val
        yield context._replace(env=new_env)


class EnvRemove(IORedirectExpression):
    def __init__(self, inner, name):
        super(EnvRemove, self).__init__(inner, "env_remove", [name])
        self._name = name

    @contextmanager
    def _update_context(self, context):
        # Pretend the IOContext is totally immutable. Copy its environment
        # dictionary instead of modifying it in place.
        new_env = context.env.copy()
        new_env.pop(self._name, None)  # avoid throwing if undefined
        yield context._replace(env=new_env)


class EnvClear(IORedirectExpression):
    def __init__(self, inner):
        super(EnvClear, self).__init__(inner, "env_clear", [])

    @contextmanager
    def _update_context(self, context):
        # Pretend the IOContext is totally immutable. Copy its environment
        # dictionary instead of modifying it in place.
        yield context._replace(env={})


# The IOContext represents the child process environment at any given point in
# the execution of an expression. We read the working directory and the entire
# environment when we create a new execution context. Methods like .env(),
# .cwd(), and .pipe() will create new modified contexts and pass those to their
# children. The IOContext does *not* own any of the file descriptors it's
# holding -- it's the caller's responsibility to close those.
IOContext = namedtuple("IOContext", [
    "stdin",
    "stdout",
    "stderr",
    "cwd",
    "env",
    "stdout_capture",
    "stderr_capture",
])


def starter_iocontext(stdout_capture, stderr_capture):
    # Hardcode the standard file descriptors. We can't rely on None here,
    # becase STDOUT/STDERR swapping needs to work.
    return IOContext(
        stdin=0,
        stdout=1,
        stderr=2,
        cwd=os.getcwd(),
        # Pretend this dictionary is immutable please.
        env=os.environ.copy(),
        stdout_capture=stdout_capture,
        stderr_capture=stderr_capture,
    )


@contextmanager
def child_stdin_pipe(stdin_arg):
    if is_pipe_already(stdin_arg):
        yield stdin_arg
    elif stdin_arg == DEVNULL:
        with open_devnull('r') as read:
            yield read
    elif could_be_path(stdin_arg):
        with open_path(stdin_arg, 'r') as read:
            yield read
    else:
        raise TypeError(
            "Not a valid stdin parameter: " + ioarg_repr(stdin_arg))


@contextmanager
def child_output_pipe(stdout_pipe, stderr_pipe, capture_pipe, output_arg):
    if is_pipe_already(output_arg):
        yield output_arg
    elif output_arg == DEVNULL:
        with open_devnull('w') as write:
            yield write
    elif output_arg == STDOUT:
        yield stdout_pipe
    elif output_arg == STDERR:
        yield stderr_pipe
    elif output_arg == CAPTURE:
        yield capture_pipe
    elif could_be_path(output_arg):
        with open_path(output_arg, 'w') as write:
            yield write
    else:
        raise TypeError(
            "Not a valid output parameter: " + ioarg_repr(output_arg))


def is_pipe_already(iovalue):
    # For files and file descriptors, we'll pass them directly to the
    # subprocess module.
    try:
        # See if the value has a fileno. Non-file buffers like StringIO have
        # the fileno method but throw UnsupportedOperation.
        iovalue.fileno()
        return True
    except (AttributeError, io.UnsupportedOperation):
        # If there's no fileno, also accept integer file descriptors.
        return isinstance(iovalue, int) and iovalue >= 0


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


def could_be_path(val):
    return is_bytes(val) or is_unicode(val) or isinstance(val, PurePath)


@contextmanager
def open_path(iovalue, mode):
    with open(stringify_if_path(iovalue), mode) as f:
        yield f


try:
    # not defined in Python 2
    PIPE_CLOSED_ERROR = BrokenPipeError
except NameError:
    PIPE_CLOSED_ERROR = IOError


@contextmanager
def spawn_input_writer(input_bytes):
    read, write = open_pipe()

    def write_thread():
        with write:
            # If the write blocks on a full pipe buffer (default 64 KB on
            # Linux), and then the program on the other end quits before
            # reading everything, the write will throw. Catch this error.
            try:
                write.write(input_bytes)
            except PIPE_CLOSED_ERROR:
                pass
    thread = ThreadWithReturn(write_thread)
    thread.start()
    with read:
        yield read
    thread.join()


@contextmanager
def spawn_output_reader():
    read, write = open_pipe()

    def read_thread():
        with read:
            return read.read()

    thread = ThreadWithReturn(read_thread)
    thread.start()
    with write:
        # We yield the thread too, so that the caller can get the str/bytes
        # iovalue it collects.
        yield write, thread
    thread.join()


def stringify_if_path(x):
    if isinstance(x, PurePath):
        return str(x)
    return x


def stringify_with_dot_if_path(x):
    '''Pathlib never renders a leading './' in front of a local path. That's an
    issue because on POSIX subprocess.py (like bash) won't execute scripts in
    the current directory without it. In the same vein, we also don't want
    Path('echo') to match '/usr/bin/echo' from the $PATH. To work around both
    issues, we explicitly join a leading dot to any relative pathlib path.'''
    if isinstance(x, PurePath):
        # Note that join does nothing if the path is absolute.
        return os.path.join('.', str(x))
    return x


def ioarg_repr(arg):
    constants = {
        STDOUT: "STDOUT",
        STDERR: "STDERR",
        DEVNULL: "DEVNULL",
        CAPTURE: "CAPTURE",
    }
    if arg in constants:
        return constants[arg]
    else:
        return repr(arg)


class ThreadWithReturn(threading.Thread):
    '''The standard Thread class doesn't give us any way to access the return
    value of the target function, or to see any exceptions that might've gotten
    thrown. This is a thin wrapper around Thread that enhances the join
    function to return values and reraise exceptions.'''
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


popen_lock = threading.Lock()


def safe_popen(*args, **kwargs):
    '''This wrapper works around two major deadlock issues to do with pipes.
    The first is that, before Python 3.2 on POSIX systems, os.pipe() creates
    inheritable file descriptors, which leak to all child processes and prevent
    reads from reaching EOF. The workaround for this is to set close_fds=True
    on POSIX, which was not the default in those versions. See PEP 0446 for
    many details.

    The second issue arises on Windows, where we're not allowed to set
    close_fds=True while also setting stdin/stdout/stderr. Descriptors from
    os.pipe() on Windows have never been inheritable, so it would seem that
    we're safe. However, the Windows implementation of subprocess.Popen()
    creates temporary inheritable copies of its descriptors, and these can
    leak. The workaround for this is to protect Popen() with a global lock. See
    https://bugs.python.org/issue25565.'''

    close_fds = (os.name != 'nt')
    with popen_lock:
        return subprocess.Popen(*args, close_fds=close_fds, **kwargs)


def decode_with_universal_newlines(b):
    '''We could let our pipes do this for us, by opening them in universal
    newlines mode, but it's a bit cleaner to do it ourselves. That saves us
    from passing around the mode all over the place, and from having decoding
    exceptions thrown on reader threads.'''
    return b.decode().replace('\r\n', '\n').replace('\r', '\n')


def encode_with_universal_newlines(s):
    return s.replace('\n', os.linesep).encode()
