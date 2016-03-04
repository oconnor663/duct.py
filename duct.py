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


# Public API
# ==========

# same as in the subprocess module
PIPE = -1
STDOUT = -2
DEVNULL = -3
# not defined in subprocess (value may change)
STDERR = -4


def cmd(prog, *args, **cmd_kwargs):
    ioargs = parse_cmd_kwargs(**cmd_kwargs)
    return Command(prog, args, ioargs)


def sh(shell_str, **cmd_kwargs):
    ioargs = parse_cmd_kwargs(**cmd_kwargs)
    return ShellCommand(shell_str, ioargs)


class Expression:
    'Abstract base class for all expression types.'

    def run(self, decode=False, sh_trim=False, **cmd_kwargs):
        '''Execute the expression and return a Result. Raise an exception if
        the returncode is non-zero, unless `check` is False.'''
        ioargs = parse_cmd_kwargs(**cmd_kwargs)
        return run_expression(self, decode, sh_trim, ioargs)

    def read(self, stdout=PIPE, decode=True, sh_trim=True, **run_kwargs):
        '''Execute the expression and capture its output, similar to backticks
        or $() in the shell. This is a wrapper around run(), which sets
        stdout=PIPE, decode=True, and sh_trim=True, and then returns
        result.stdout instead of the whole result.'''
        result = self.run(stdout=stdout, decode=decode, sh_trim=sh_trim,
                          **run_kwargs)
        return result.stdout

    def pipe(self, right_side):
        return Pipe(self, right_side)

    def then(self, right_side):
        return Then(self, right_side)

    def subshell(self, **cmd_kwargs):
        '''For applying IO arguments to an entire expression. Normally you do
        this with arguments to run(), but sometimes you need to do further
        composition. For example:

            some_expression.subshell(stderr=STDOUT).pipe(another_expression)
        '''
        ioargs = parse_cmd_kwargs(**cmd_kwargs)
        return Subshell(self, ioargs)


Result = namedtuple('Result', ['returncode', 'stdout', 'stderr'])


class CheckedError(subprocess.CalledProcessError):
    def __init__(self, result, expression):
        self.result = result
        self.expression = expression

    def __str__(self):
        return 'Expression {0} returned non-zero exit status {1}.'.format(
            self.expression, self.result.returncode)


# Implementation Details
# ======================

# Set up any readers or writers, kick off the recurisve _exec(), and collect
# the results. This is the core of the execution methods, run() and read().
def run_expression(expr, decode, sh_trim, ioargs):
    default_iocontext = IOContext()
    with default_iocontext.child_context(ioargs) as iocontext:
        # Kick off the child processes.
        returncode = expr._exec(iocontext)
    stdout_result = process_output_result(
        iocontext.stdout_result(), decode, sh_trim)
    stderr_result = process_output_result(
        iocontext.stderr_result(), decode, sh_trim)
    result = Result(returncode, stdout_result, stderr_result)
    if ioargs.check and returncode != 0:
        raise CheckedError(result, expr)
    return result


def process_output_result(result, decode, sh_trim):
    '''This function takes care of decoding Unicode bytes, universalizing
    newlines, and trimming tailing newlines, as appropriate.'''
    if result is None:
        return None
    if not decode:
        return result
    decoded_result = decode_with_universal_newlines(result)
    if sh_trim:
        decoded_result = decoded_result.rstrip('\n')
    return decoded_result


class Command(Expression):
    def __init__(self, prog, args, ioargs):
        '''The prog and args will be passed directly to subprocess.call(),
        which determines the types allowed here (strings and bytes). In
        addition, we also explicitly support pathlib Paths, by converting them
        to strings.'''
        prog_str = stringify_with_dot_if_path(prog)
        args_strs = tuple(stringify_if_path(arg) for arg in args)
        self._tuple = (prog_str,) + args_strs
        self._ioargs = ioargs

    def _exec(self, parent_iocontext):
        with parent_iocontext.child_context(self._ioargs) as iocontext:
            cwd = stringify_if_path(iocontext.cwd)
            full_env = stringify_paths_in_dict(iocontext.full_env)
            proc = safe_popen(
                self._tuple, cwd=cwd, env=full_env, stdin=iocontext.stdin_pipe,
                stdout=iocontext.stdout_pipe, stderr=iocontext.stderr_pipe)
        returncode = proc.wait()
        return returncode if self._ioargs.check else 0

    def __repr__(self):
        return expression_repr('cmd', self._tuple, self._ioargs)


class ShellCommand(Expression):
    def __init__(self, shell_cmd, ioargs):
        # The command could be a Path. This is potentially useful on Windows
        # where you have to run things like .py files in shell mode.
        self._shell_cmd = stringify_with_dot_if_path(shell_cmd)
        self._ioargs = ioargs

    def _exec(self, parent_iocontext):
        with parent_iocontext.child_context(self._ioargs) as iocontext:
            cwd = stringify_if_path(iocontext.cwd)
            full_env = stringify_paths_in_dict(iocontext.full_env)
            proc = safe_popen(
                self._shell_cmd, shell=True, cwd=cwd, env=full_env,
                stdin=iocontext.stdin_pipe, stdout=iocontext.stdout_pipe,
                stderr=iocontext.stderr_pipe)
        returncode = proc.wait()
        return returncode if self._ioargs.check else 0

    def __repr__(self):
        return expression_repr('sh', [self._shell_cmd], self._ioargs)


class Subshell(Expression):
    def __init__(self, expr, ioargs):
        self._expr = expr
        self._ioargs = ioargs

    def _exec(self, parent_iocontext):
        with parent_iocontext.child_context(self._ioargs) as iocontext:
            returncode = self._expr._exec(iocontext)
        return returncode if self._ioargs.check else 0

    def __repr__(self):
        return repr(self._expr) + '.' + expression_repr(
            'subshell', [], self._ioargs)


class Then(Expression):
    def __init__(self, left, right):
        self._left = left
        self._right = right

    def _exec(self, parent_iocontext):
        # Execute the first command.
        left_status = self._left._exec(parent_iocontext)
        # If it returns non-zero short-circuit.
        if left_status != 0:
            return left_status
        # Otherwise execute the second command.
        right_status = self._right._exec(parent_iocontext)
        return right_status

    def __repr__(self):
        return "{}.then({})".format(repr(self._left), repr(self._right))


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

    def _exec(self, parent_iocontext):
        # Open a read/write pipe. The write end gets passed to the left as
        # stdout, and the read end gets passed to the right as stdin. Either
        # side could be a compound expression (like A.then(B)), so we have to
        # wait until each command is completely finished before we can close
        # its end of the pipe. Closing the write end allows the right side to
        # receive EOF, and closing the read end allows the left side to receive
        # SIGPIPE.
        read_pipe, write_pipe = open_pipe()

        def do_left():
            left_ioargs = parse_cmd_kwargs(stdout=write_pipe)
            left_iocm = parent_iocontext.child_context(left_ioargs)
            with write_pipe:
                with left_iocm as iocontext:
                    return self._left._exec(iocontext)
        left_thread = ThreadWithReturn(target=do_left)
        left_thread.start()

        right_ioargs = parse_cmd_kwargs(stdin=read_pipe)
        right_iocm = parent_iocontext.child_context(right_ioargs)
        with read_pipe:
            with right_iocm as iocontext:
                right_status = self._right._exec(iocontext)
        left_status = left_thread.join()

        # Return the rightmost error, if any. Note that cwd and env changes
        # never propagate out of the pipe. This is the same behavior as bash.
        if right_status != 0:
            return right_status
        else:
            return left_status

    def __repr__(self):
        return "{}.pipe({})".format(repr(self._left), repr(self._right))


def trim_if_string(x):
    '''Trim trailing newlines, as the shell does by default when it's capturing
    output. Only do this for strings, because it's likely to be a mistake when
    used with bytes. For example:
        # Does the user want this trimmed, or did they just forget
        # sh_trim=False?
        cmd('head -c 10 /dev/urandom').read(stdout=bytes)
    '''
    # Check for str in Python 3, unicode in Python 2.
    if isinstance(x, type(u'')):
        return x.rstrip('\n')
    else:
        return x


def open_pipe():
    read_fd, write_fd = os.pipe()
    read_mode, write_mode = ('rb', 'wb')
    return os.fdopen(read_fd, read_mode), os.fdopen(write_fd, write_mode)


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


IOArgs = namedtuple('IOArgs', ['input', 'stdin', 'stdout', 'stderr', 'cwd',
                               'env', 'full_env', 'check'])


def parse_cmd_kwargs(input=None, stdin=None, stdout=None, stderr=None,
                     cwd=None, env=None, full_env=None, check=True):
    '''We define this constructor function so that we can do early error
    checking on IO arguments. Other constructors can pass their keyword args
    here as part of __init__, rather than holding them until _exec, so that
    invalid keywords cause errors in the right place. This also lets us do some
    consistency checks early, like prohibiting using input and stdin at the
    same time.

    If we only needed to support Python 3, we might handle the "check" keyword
    arg separately from this function. It's not intended to be inherited by
    subexpressions, so it doesn't live in the IOArgs. However, Python 2 doesn't
    support syntax like this:

        def f(*args, check=True, **kwargs):  # only valid in Python 3!

    So we have to handle "check" in the same kwargs dict, and parse it out
    here.'''
    if input is not None and stdin is not None:
        raise ValueError('stdin and input arguments may not both be used.')
    if env is not None and full_env is not None:
        raise ValueError('env and full_env arguments may not both be used.')
    return IOArgs(input, stdin, stdout, stderr, cwd, env, full_env, check)


class IOContext:
    '''Both run methods and individual expressions might need to open files or
    kick off IO threads, depending on the parameters given. This class both
    interprets IO parameters and acts as a context manager for the resources it
    opens.'''

    def __init__(self, stdin_pipe=None, stdout_pipe=None, stdout_reader=None,
                 stderr_pipe=None, stderr_reader=None, cwd=None,
                 full_env=None):
        self.stdin_pipe = stdin_pipe
        self.stdout_pipe = stdout_pipe
        self.stderr_pipe = stderr_pipe
        self.cwd = cwd
        self.full_env = full_env
        # The two reader members are threads. After an IOContext is exited
        # (that is, after the close of the with-block that starts it),
        # stdout_result() and stderr_result() expose the values returned by
        # these threads.
        self._stdout_reader = stdout_reader
        self._stderr_reader = stderr_reader

    def stdout_result(self):
        if self._stdout_reader is None:
            return None
        if self._stdout_reader.is_alive():
            raise RuntimeError("The stdout reader is still alive.")
        return self._stdout_reader.join()

    def stderr_result(self):
        if self._stderr_reader is None:
            return None
        if self._stderr_reader.is_alive():
            raise RuntimeError("The stderr reader is still alive.")
        return self._stderr_reader.join()

    @contextmanager
    def child_context(self, ioargs):
        '''This is the top level context manager for any files we open or
        threads we spawn. This is used both when we kick off execution of a
        whole expression, and when individual parts of that expression
        interpret their IO arguments. Values like None and STDOUT are
        interpreted relative to the parent context (that is, self). The either
        the new child context gets passed to subexpressions, or the pipes it
        holds are used to execute a real command.'''
        cwd = self.cwd if ioargs.cwd is None else ioargs.cwd
        full_env = make_full_env(self.full_env, ioargs.env, ioargs.full_env)
        stdin_cm = child_input_pipe(
            self.stdin_pipe, ioargs.input, ioargs.stdin)
        stdout_cm = child_output_pipe(self.stdout_pipe, ioargs.stdout)
        stderr_cm = child_output_pipe(self.stderr_pipe, ioargs.stderr)
        with stdin_cm as stdin_pipe:
            with stdout_cm as (pre_swap_stdout_pipe, stdout_reader):
                with stderr_cm as (pre_swap_stderr_pipe, stderr_reader):
                    stdout_pipe, stderr_pipe = apply_swaps(
                        ioargs.stdout, ioargs.stderr,
                        pre_swap_stdout_pipe, pre_swap_stderr_pipe)
                    yield IOContext(stdin_pipe, stdout_pipe, stdout_reader,
                                    stderr_pipe, stderr_reader, cwd, full_env)


# Yields a read pipe.
@contextmanager
def child_input_pipe(parent_pipe, input_arg, stdin_arg):
    # Check the input parameter first, because stdin will be None if input is
    # set, and we don't want to short circuit. (None is an otherwise valid
    # stdin value, meaning "inherit the current context's stdin".)
    if wants_input_writer(input_arg):
        with spawn_input_writer(input_arg) as read:
            yield read
    elif input_arg is not None:
        raise TypeError("Not a valid input parameter: " + repr(input_arg))
    elif stdin_arg is None:
        yield parent_pipe
    elif is_pipe_already(stdin_arg):
        yield stdin_arg
    elif is_devnull(stdin_arg):
        with open_devnull('r') as read:
            yield read
    elif is_path(stdin_arg):
        with open_path(stdin_arg, 'r') as read:
            yield read
    else:
        raise TypeError("Not a valid stdin parameter: " + repr(stdin_arg))


# Yields both a write pipe and an optional output reader thread.
@contextmanager
def child_output_pipe(default_pipe, output_arg):
    # Swap flags (STDOUT, STDERR) have to be handled in a later step, because
    # e.g. the new stderr pipe won't be ready yet when we're preparing stdout.
    if output_arg is None or is_swap(output_arg):
        yield default_pipe, None
    elif is_pipe_already(output_arg):
        yield output_arg, None
    elif is_devnull(output_arg):
        with open_devnull('w') as write:
            yield write, None
    elif is_path(output_arg):
        with open_path(output_arg, 'w') as write:
            yield write, None
    elif wants_output_reader(output_arg):
        with spawn_output_reader() as (write, thread):
            yield write, thread
    else:
        raise TypeError("Not a valid output parameter: " + repr(output_arg))


def is_swap(output_arg):
    return output_arg in (STDOUT, STDERR)


def apply_swaps(stdout_arg, stderr_arg, stdout_pipe, stderr_pipe):
    # Note that stdout=STDOUT and stderr=STDERR are no-ops.
    new_stdout = stderr_pipe if stdout_arg == STDERR else stdout_pipe
    new_stderr = stdout_pipe if stderr_arg == STDOUT else stderr_pipe
    return new_stdout, new_stderr


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


def is_devnull(iovalue):
    return iovalue == DEVNULL


@contextmanager
def open_devnull(mode):
    # We open devnull ourselves because Python 2 doesn't support DEVNULL.
    with open(os.devnull, mode) as f:
        yield f


def is_path(iovalue):
    return isinstance(iovalue, (str, bytes, PurePath))


@contextmanager
def open_path(iovalue, mode):
    with open(stringify_if_path(iovalue), mode) as f:
        yield f


def wants_input_writer(input_arg):
    return isinstance(input_arg, (str, bytes))


try:
    # not defined in Python 2
    PIPE_CLOSED_ERROR = BrokenPipeError
except NameError:
    PIPE_CLOSED_ERROR = IOError


@contextmanager
def spawn_input_writer(input_arg):
    read, write = open_pipe()

    def write_thread():
        # If the argument is a string, convert it to bytes first.
        if isinstance(input_arg, str):
            input_bytes = encode_with_universal_newlines(input_arg)
        else:
            input_bytes = input_arg

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


def wants_output_reader(output_arg):
    return output_arg == PIPE


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


def make_full_env(parent_env, env, full_env):
    '''We support the 'env' parameter to add environment variables to the
    default environment (this differs from subprocess's standard behavior, but
    it's by far the most common use case), and the 'full_env' parameter to
    supply the entire environment. Callers shouldn't supply both in one place,
    but it's possible for parameters on individual commands to edit or override
    what's given to run().'''
    if full_env is not None:
        return full_env
    ret = os.environ.copy() if parent_env is None else parent_env.copy()
    if env is not None:
        ret.update(env)
    return ret


def stringify_if_path(x):
    if isinstance(x, PurePath):
        return str(x)
    return x


def stringify_paths_in_dict(d):
    return dict((stringify_if_path(key), stringify_if_path(val))
                for key, val in d.items())


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


def expression_repr(name, args, ioargs, **kwargs):
    '''Handle all the shared logic for printing expression arguments.'''
    constants = {
        PIPE: "PIPE",
        STDOUT: "STDOUT",
        DEVNULL: "DEVNULL",
        STDERR: "STDERR",
    }
    parts = [repr(i) for i in args]
    kwargs.update(ioargs._asdict())
    # Assume any keywords not listed default to None.
    keyword_defaults = {'check': True}
    # Only print fields with a non-default value. Also sort the keys
    # alphabetically, so that the repr is stable for testing.
    for key, val in sorted(kwargs.items()):
        if isinstance(val, int) and val < 0:
            # A duct constant. Print its name.
            val_repr = constants[val]
        else:
            val_repr = repr(val)
        if val != keyword_defaults.get(key, None):
            parts.append(key + '=' + val_repr)
    return name + '(' + ', '.join(parts) + ')'


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
