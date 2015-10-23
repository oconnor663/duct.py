from collections import namedtuple
from contextlib import contextmanager
import io
import os
import pathlib
import re
import subprocess
import threading

# Public API
# ==========

# same as in the subprocess module
STDOUT = -2
DEVNULL = -3
# not defined in subprocess (values may change)
STDERR = -4


def cmd(prog, *args, check=True, **io_kwargs):
    ioargs = make_ioargs(**io_kwargs)
    return Command(prog, args, check, ioargs)


def sh(shell_str, check=True, **io_kwargs):
    ioargs = make_ioargs(**io_kwargs)
    return Shell(shell_str, check, ioargs)


class Expression:
    'Abstract base class for all expression types.'

    def run(self, check=True, trim=False, **io_kwargs):
        ioargs = make_ioargs(**io_kwargs)
        return run_expression(self, check, trim, ioargs)

    def read(self, stdout=str, trim=True, **run_kwargs):
        result = self.run(stdout=stdout, trim=trim, **run_kwargs)
        return result.stdout

    def pipe(self, *cmd, **cmd_kwargs):
        return Pipe(self, command_or_parts(*cmd, **cmd_kwargs))

    def then(self, *cmd, **cmd_kwargs):
        return Then(self, command_or_parts(*cmd, **cmd_kwargs))


# Implementation Details
# ======================

# Set up any readers or writers, kick off the recurisve _exec(), and collect
# the results. This is the core of the execution methods, run() and read().
def run_expression(expr, check, trim, ioargs):
    default_iocontext = IOContext()
    with default_iocontext.child_context(ioargs) as iocontext:
        # Kick off the child processes.
        status = expr._exec(iocontext)
    stdout_result = iocontext.stdout_result()
    stderr_result = iocontext.stderr_result()
    if trim:
        stdout_result = trim_if_string(stdout_result)
        stderr_result = trim_if_string(stderr_result)
    result = Result(status, stdout_result, stderr_result)
    if check and status != 0:
        raise CheckedError(result, expr)
    return result


# Methods like pipe() take a command argument. This can either be arguments to
# a Command constructor, or it can be an already-fully-formed command, like
# another compount expression or the output of sh().
def command_or_parts(first, *rest, **cmd_kwargs):
    if isinstance(first, Expression):
        if rest or cmd_kwargs:
            raise TypeError("When an expression object is given, "
                            "no other arguments are allowed.")
        return first
    return cmd(first, *rest, **cmd_kwargs)


class Command(Expression):
    def __init__(self, prog, args, check, ioargs):
        '''The prog and args will be passed directly to subprocess.call(),
        which determines the types allowed here (strings and bytes). In
        addition, we also explicitly support pathlib Paths, by converting them
        to strings.'''
        self._tuple = (prog,) + args
        self._check = check
        self._ioargs = ioargs

    def _exec(self, parent_iocontext):
        with parent_iocontext.child_context(self._ioargs) as iocontext:
            command = stringify_paths_in_list(self._tuple)
            cwd = stringify_if_path(iocontext.cwd)
            full_env = stringify_paths_in_dict(iocontext.full_env)
            status = subprocess.call(
                command, stdin=iocontext.stdin_pipe,
                stdout=iocontext.stdout_pipe, stderr=iocontext.stderr_pipe,
                cwd=cwd, env=full_env)
        return status if self._check else 0

    def __repr__(self):
        quoted_parts = []
        for part in self._tuple:
            # Decode bytes.
            if not isinstance(part, str):
                part = part.decode()
            # Quote strings that have whitespace.
            if re.search(r'\s+', part):
                part = '"' + part + '"'
            quoted_parts.append(part)
        return ' '.join(quoted_parts)


class Shell(Expression):
    def __init__(self, shell_cmd, check, ioargs):
        # The command could be a Path. This is potentially useful on Windows
        # where you have to run things like .py files in shell mode.
        self._shell_cmd = shell_cmd
        self._check = check
        self._ioargs = ioargs

    def _exec(self, parent_iocontext):
        with parent_iocontext.child_context(self._ioargs) as iocontext:
            shell_str = stringify_if_path(self._shell_cmd)
            cwd = stringify_if_path(iocontext.cwd)
            full_env = stringify_paths_in_dict(iocontext.full_env)
            status = subprocess.call(
                shell_str, shell=True, stdin=iocontext.stdin_pipe,
                stdout=iocontext.stdout_pipe, stderr=iocontext.stderr_pipe,
                cwd=cwd, env=full_env)
        return status if self._check else 0

    def __repr__(self):
        # TODO: This should do some escaping.
        return str(self._shell_cmd)


class CompoundExpression(Expression):
    # subclasses must override this
    _repr_divider = None

    def __init__(self, left, right):
        self._left = left
        self._right = right

    def __repr__(self):
        parts = []
        for part in (self._left, self._right):
            if (isinstance(part, CompoundExpression) and
                    not isinstance(part, self.__class__)):
                parts.append('(' + repr(part) + ')')
            else:
                parts.append(repr(part))
        return self._repr_divider.join(parts)


class Then(CompoundExpression):
    _repr_divider = ' && '

    def _exec(self, parent_iocontext):
        # Execute the first command.
        left_status = self._left._exec(parent_iocontext)
        # If it returns non-zero short-circuit.
        if left_status != 0:
            return left_status
        # Otherwise execute the second command.
        right_status = self._right._exec(parent_iocontext)
        return right_status


# Pipe uses another thread to run the left side of the pipe in parallel with
# the right. This is required because both the left and the right might be
# compound expressions, where a second command might need to be started after
# the first finishes, so someone has to be waiting on both sides at the same
# time. There are Unix-specific ways to wait on multiple processes at once, but
# those can conflict with other listeners that might by in the same process,
# and they won't work on Windows anyway.
class Pipe(CompoundExpression):
    _repr_divider = ' || '

    def _exec(self, parent_iocontext):
        # Open a read/write pipe. The write end gets passed to the left as
        # stdout, and the read end gets passed to the right as stdin. Either
        # side could be a compound expression (like A.then(B)), so we have to
        # wait until each command is completely finished before we can close
        # its end of the pipe. Closing the write end allows the right side to
        # receive EOF, and closing the read end allows the left side to receive
        # SIGPIPE.
        read_pipe, write_pipe = open_pipe(binary=True)

        def do_left():
            left_ioargs = make_ioargs(stdout=write_pipe)
            left_iocm = parent_iocontext.child_context(left_ioargs)
            with write_pipe, left_iocm as iocontext:
                return self._left._exec(iocontext)
        left_thread = ThreadWithReturn(target=do_left)
        left_thread.start()

        right_ioargs = make_ioargs(stdin=read_pipe)
        right_iocm = parent_iocontext.child_context(right_ioargs)
        with read_pipe, right_iocm as iocontext:
            right_status = self._right._exec(iocontext)
        left_status = left_thread.join()

        # Return the rightmost error, if any. Note that cwd and env changes
        # never propagate out of the pipe. This is the same behavior as bash.
        if right_status != 0:
            return right_status
        else:
            return left_status


Result = namedtuple('Result', ['status', 'stdout', 'stderr'])


class CheckedError(subprocess.CalledProcessError):
    def __init__(self, result, command):
        self.result = result
        self.command = command

    def __str__(self):
        return 'Command "{}" returned non-zero exit status {}.'.format(
            self.command, self.result.status)


def trim_if_string(x):
    '''Trim trailing newlines, as the shell does by default when it's capturing
    output. Only do this for strings, because it's likely to be a mistake when
    used with bytes. For example:
        # Does the user want this trimmed, or did they just forget trim=False?
        cmd('head -c 10 /dev/urandom').read(stdout=bytes)
    '''
    if isinstance(x, str):
        return x.rstrip('\n')
    else:
        return x


def open_pipe(binary=False):
    read_fd, write_fd = os.pipe()
    read_mode, write_mode = ('rb', 'wb') if binary else ('r', 'w')
    return os.fdopen(read_fd, read_mode), os.fdopen(write_fd, write_mode)


class ThreadWithReturn(threading.Thread):
    '''The standard Thread class doesn't give us any way to access the return
    value of the target function, or to see any exceptions that might've gotten
    thrown. This is a thin wrapper around Thread that enhances the join
    function to return values and reraise exceptions.'''
    def __init__(self, target, args=(), kwargs={}, **thread_kwargs):
        threading.Thread.__init__(self, **thread_kwargs)
        self._target = target
        self._args = args
        self._kwargs = kwargs
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


_IOArgs = namedtuple('_IOArgs', ['input', 'stdin', 'stdout', 'stderr', 'cwd',
                                 'env', 'full_env'])


def make_ioargs(input=None, stdin=None, stdout=None, stderr=None, cwd=None,
                env=None, full_env=None):
    '''We define this constructor function so that we can do early error
    checking on IO arguments. Other constructors can pass their keyword args
    here as part of __init__, rather than holding them until _exec, so that
    invalid keywords cause errors in the right place. This also lets us do some
    consistency checks early, like prohibiting both input and stdin at the same
    time.'''
    if input is not None and stdin is not None:
        raise ValueError('stdin and input arguments may not both be used.')
    if env is not None and full_env is not None:
        raise ValueError('env and full_env arguments may not both be used.')
    return _IOArgs(input, stdin, stdout, stderr, cwd, env, full_env)


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
        cwd = self.cwd if ioargs.cwd is None else ioargs.cwd
        full_env = make_full_env(self.full_env, ioargs.env, ioargs.full_env)
        stdin_cm = child_input_pipe(
            self.stdin_pipe, ioargs.input, ioargs.stdin)
        stdout_cm = child_output_pipe(
            self.stdout_pipe, self.stderr_pipe, self.stdout_pipe,
            ioargs.stdout)
        stderr_cm = child_output_pipe(
            self.stdout_pipe, self.stderr_pipe, self.stderr_pipe,
            ioargs.stderr)
        with stdin_cm as stdin_pipe, \
                stdout_cm as (stdout_pipe, stdout_reader), \
                stderr_cm as (stderr_pipe, stderr_reader):
            yield IOContext(stdin_pipe, stdout_pipe, stdout_reader,
                            stderr_pipe, stderr_reader, cwd, full_env)


# Yields a read pipe.
@contextmanager
def child_input_pipe(parent_pipe, input_arg, stdin_arg):
    # Check the input parameter first, because stdin will be None if input is
    # set, and we don't want to short circuit. (None is an otherwise valid
    # stdin value, meaning "inherit the current context's stdin".)
    if input_arg is not None and stdin_arg is not None:
        raise ValueError('stdin and input arguments may not both be used.')
    elif wants_input_writer(input_arg):
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
def child_output_pipe(parent_stdout, parent_stderr, default_pipe, output_arg):
    if output_arg is None:
        yield default_pipe, None
    elif is_pipe_already(output_arg):
        yield output_arg, None
    elif is_swap(output_arg):
        yield get_swapped_pipe(parent_stdout, parent_stderr, output_arg), None
    elif is_devnull(output_arg):
        with open_devnull('w') as write:
            yield write, None
    elif is_path(output_arg):
        with open_path(output_arg, 'w') as write:
            yield write, None
    elif wants_output_reader(output_arg):
        with spawn_output_reader(output_arg) as (write, thread):
            yield write, thread
    else:
        raise TypeError("Not a valid output parameter: " + repr(output_arg))


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


def is_swap(output_arg):
    return output_arg == STDOUT or output_arg == STDERR


def get_swapped_pipe(parent_stdout, parent_stderr, output_arg):
    if output_arg == STDOUT:
        return parent_stdout
    if output_arg == STDERR:
        return parent_stderr


def is_devnull(iovalue):
    return iovalue == DEVNULL


@contextmanager
def open_devnull(mode):
    # We open devnull ourselves because Python 2 doesn't support DEVNULL.
    with open(os.devnull, mode) as f:
        yield f


def is_path(iovalue):
    return isinstance(iovalue, (str, bytes, pathlib.PurePath))


@contextmanager
def open_path(iovalue, mode):
    with open(stringify_if_path(iovalue), mode) as f:
        yield f


def wants_input_writer(input_arg):
    return isinstance(input_arg, (str, bytes))


@contextmanager
def spawn_input_writer(input_arg):
    read, write = open_pipe(binary=isinstance(input_arg, bytes))

    def write_thread():
        with write:
            write.write(input_arg)
    # Nothing ever needs to join this thread. It terminates either when it's
    # done writing, or when its pipe closes.
    thread = ThreadWithReturn(write_thread)
    thread.start()
    with read:
        yield read
    thread.join()


def wants_output_reader(output_arg):
    return output_arg is str or output_arg is bytes


@contextmanager
def spawn_output_reader(output_arg):
    read, write = open_pipe(binary=output_arg is bytes)

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
    what's given to run(). We also convert pathlib Paths to strings.'''
    if full_env is not None:
        return full_env
    ret = os.environ.copy() if parent_env is None else parent_env.copy()
    if env is not None:
        ret.update(env)
    return ret


def stringify_if_path(x):
    if isinstance(x, pathlib.PurePath):
        return str(x)
    return x


def stringify_paths_in_list(l):
    return [stringify_if_path(x) for x in l]


def stringify_paths_in_dict(d):
    return {stringify_if_path(key): stringify_if_path(val)
            for key, val in d.items()}
