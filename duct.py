import collections
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


def cmd(prog, *args, **kwargs):
    return Command(prog, *args, **kwargs)


def sh(shell_cmd, **kwargs):
    return Shell(shell_cmd, **kwargs)


class Expression:
    'Abstract base class for all expression types.'

    def run(self, input=None, stdin=None, stdout=None, stderr=None, check=True,
            trim=False, cwd=None, env=None, full_env=None):
        return run(self, input, stdin, stdout, stderr, trim, check, cwd, env,
                   full_env)

    def read(self, stdout=str, trim=True, **kwargs):
        result = self.run(stdout=stdout, trim=trim, **kwargs)
        return result.stdout

    def pipe(self, *cmd, **kwargs):
        return Pipe(self, command_or_parts(*cmd, **kwargs))

    def then(self, *cmd, **kwargs):
        return Then(self, command_or_parts(*cmd, **kwargs))


# Implementation Details
# ======================

# Set up any readers or writers, kick off the recurisve _exec(), and collect
# the results. This is the core of the three execution methods: run(), read(),
# and result().
def run(expr, input, stdin, stdout, stderr, trim, check, cwd, env, full_env):
    full_env = update_env(None, env, full_env)
    iocontext = IOContext(None, None, None, input, stdin, stdout, stderr)
    with iocontext as (stdin_pipe, stdout_pipe, stderr_pipe):
        # Kick off the child processes.
        status = expr._exec(stdin_pipe, stdout_pipe, stderr_pipe, cwd,
                            full_env)
    stdout_result = iocontext.stdout_result
    stderr_result = iocontext.stderr_result
    if trim:
        stdout_result = trim_if_string(iocontext.stdout_result)
        stderr_result = trim_if_string(iocontext.stderr_result)
    result = Result(status, stdout_result, stderr_result)
    if check and status != 0:
        raise CheckedError(result, expr)
    return result


# Methods like pipe() take a command argument. This can either be arguments to
# a Command constructor, or it can be an already-fully-formed command, like
# another compount expression or the output of sh().
def command_or_parts(first, *rest, **kwargs):
    if isinstance(first, Expression):
        if rest or kwargs:
            raise TypeError("When an expression object is given, "
                            "no other arguments are allowed.")
        return first
    return Command(first, *rest, **kwargs)


class CommandBase(Expression):
    '''Base class for both Command (which takes a program name and a list of
    arguments) and Shell (which takes a string and runs it with shell=True).
    Handles shared options.'''
    def __init__(self, check=True, cwd=None, env=None, full_env=None):
        self._check = check
        self._cwd = cwd
        self._env = env
        self._full_env = full_env

    # for subclasses
    def _run_subprocess():
        raise NotImplementedError

    def _exec(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd, full_env):
        # Support for Path values.
        cwd = stringify_if_path(self._cwd or cwd)
        full_env = update_env(full_env, self._env, self._full_env)
        status = self._run_subprocess(
            stdin_pipe, stdout_pipe, stderr_pipe, cwd, full_env)
        if not self._check:
            status = 0
        return status


class Command(CommandBase):
    def __init__(self, prog, *args, **kwargs):
        '''The prog and args will be passed directly to subprocess.call(),
        which determines the types allowed here (strings and bytes). In
        addition, we also explicitly support pathlib Paths, by converting them
        to strings.'''
        super().__init__(**kwargs)
        self._tuple = tuple(stringify_paths_in_list((prog,) + args))

    def _run_subprocess(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd,
                        full_env):
        status = subprocess.call(
            self._tuple, stdin=stdin_pipe, stdout=stdout_pipe,
            stderr=stderr_pipe, cwd=cwd, env=full_env)
        return status

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


class Shell(CommandBase):
    def __init__(self, shell_cmd, **kwargs):
        super().__init__(**kwargs)
        self._shell_cmd = shell_cmd

    def _run_subprocess(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd,
                        full_env):
        status = subprocess.call(
            self._shell_cmd, shell=True, stdin=stdin_pipe, stdout=stdout_pipe,
            stderr=stderr_pipe, cwd=cwd, env=full_env)
        return status

    def __repr__(self):
        # TODO: This should do some escaping.
        return self._shell_cmd


class CompoundExpression(Expression):
    def __init__(self, left, right):
        self._left = left
        self._right = right


class Then(CompoundExpression):
    def _exec(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd, full_env):
        # Execute the first command.
        left_status = self._left._exec(
            stdin_pipe, stdout_pipe, stderr_pipe, cwd, full_env)
        # If it returns non-zero short-circuit.
        if left_status != 0:
            return left_status
        # Otherwise execute the second command.
        right_status = self._right._exec(
            stdin_pipe, stdout_pipe, stderr_pipe, cwd, full_env)
        return right_status

    def __repr__(self):
        return join_with_maybe_parens(self._left, self._right, ' && ', Pipe)


# Pipe uses another thread to run the left side of the pipe in parallel with
# the right. This is required because both the left and the right might be
# compound expressions, where a second command might need to be started after
# the first finishes, so someone has to be waiting on both sides at the same
# time. There are Unix-specific ways to wait on multiple processes at once, but
# those can conflict with other listeners that might by in the same process,
# and they won't work on Windows anyway.
class Pipe(CompoundExpression):
    def _exec(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd, full_env):
        # Open a read/write pipe. The write end gets passed to the left as
        # stdout, and the read end gets passed to the right as stdin. Either
        # side could be a compound expression (like A.then(B)), so we have to
        # wait until each command is completely finished before we can close
        # its end of the pipe. Closing the write end allows the right side to
        # receive EOF, and closing the read end allows the left side to receive
        # SIGPIPE.
        read_pipe, write_pipe = open_pipe(binary=True)

        def do_left():
            with write_pipe:
                return self._left._exec(
                    stdin_pipe, write_pipe, stderr_pipe, cwd, full_env)
        left_thread = ThreadWithReturn(target=do_left)
        left_thread.start()

        with read_pipe:
            right_status = self._right._exec(
                read_pipe, stdout_pipe, stderr_pipe, cwd, full_env)
        left_status = left_thread.join()

        # Return the rightmost error, if any. Note that cwd and env changes
        # never propagate out of the pipe. This is the same behavior as bash.
        if right_status != 0:
            return right_status
        else:
            return left_status

    def __repr__(self):
        return join_with_maybe_parens(
            self._left, self._right, ' | ', Then)


Result = collections.namedtuple('Result', ['status', 'stdout', 'stderr'])


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


def join_with_maybe_parens(left, right, joiner, paren_types):
    parts = []
    for part in (left, right):
        if isinstance(part, paren_types):
            parts.append('(' + repr(part) + ')')
        else:
            parts.append(repr(part))
    return joiner.join(parts)


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


class IOContext:
    '''Both run methods and individual expressions might need to open files or
    kick off IO threads, depending on the parameters given. This class both
    interprets IO parameters and acts as a context manager for the resources it
    opens.'''

    def __init__(self, stdin_pipe, stdout_pipe, stderr_pipe, input, stdin,
                 stdout, stderr):
        '''The pipe arguments are the defaults at the current point in an
        expression. In the run methods, they're given as None, which means (as
        per subprocess) the descriptors inherited from the parent process. In
        nested expressions, these might also reflect changes made by containing
        expressions. These are ultimately passed to subprocess.Popen(), so they
        should be either None or a file/descriptor.

        The rest of the arguments are the named parameters given for the
        current expression. Pathlib Paths or strings are interpreted as
        filepaths and opened in the appropriate mode. The `str` and `bytes`
        types kick off an output pipe and a reader thread, and a string or
        bytes value given as `input` kicks off an input pipe and a writer
        thread. DEVNULL is handled by opening os.devnull (because Python 2's
        subprocess doesn't support that flag directly), and the STDOUT and
        STDERR flags refer to the default pipes above.'''

        if input is not None and stdin is not None:
            raise ValueError('stdin and input arguments may not both be used.')

        self._stdin_pipe = stdin_pipe
        self._stdout_pipe = stdout_pipe
        self._stderr_pipe = stderr_pipe
        self._input = input
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr

        self._enter_pipes = [stdin_pipe, stdout_pipe, stderr_pipe]
        self._open_files = []
        self._running_threads = []

        self.stdout_result = None
        self.stderr_result = None

    def __enter__(self):
        # TODO: Handle exceptions that happen in here, like opening nonexistent
        # filepaths.
        self._handle_STDOUT_STDERR()
        self._handle_DEVNULL()
        self._handle_paths()
        self._handle_input_writer()
        self._handle_output_readers()
        return tuple(self._enter_pipes)

    def _handle_STDOUT_STDERR(self):
        # Note that stdout=STDOUT and stderr=STDERR are no-ops.
        if self._stdout == STDERR:
            self._enter_pipes[1] = self._setderr_pipe
        if self._stderr == STDOUT:
            self._enter_pipes[2] = self._stdout_pipe

    def _handle_DEVNULL(self):
        # We do this because Python 2 doesn't support DEVNULL natively.
        for param, pipe_index in \
                ((self._stdin, 0), (self._stdout, 1), (self._stderr, 2)):
            if param == DEVNULL:
                f = open(os.devnull, 'r' if pipe_index == 0 else 'w')
                self._open_files.append(f)
                self._enter_pipes[pipe_index] = f

    def _handle_paths(self):
        for param, pipe_index in \
                ((self._stdin, 0), (self._stdout, 1), (self._stderr, 2)):
            maybe_path = stringify_if_path(param)
            if isinstance(maybe_path, (str, bytes)):
                f = open(maybe_path, 'r' if pipe_index == 0 else 'w')
                self._open_files.append(f)
                self._enter_pipes[pipe_index] = f

    def _handle_input_writer(self):
        if self._input is None:
            return
        read, write = open_pipe(binary=isinstance(self._input, bytes))

        def write_thread():
            with write:
                write.write(self._input)

        thread = ThreadWithReturn(write_thread)
        thread.start()
        self._running_threads.append(thread)
        self._open_files.append(read)
        self._enter_pipes[0] = read

    def _handle_output_readers(self):
        for param, pipe_index, attr in ((self._stdout, 1, "stdout_result"),
                                        (self._stderr, 2, "stderr_result")):
            if param in (str, bytes):
                read, write = open_pipe(binary=param is bytes)

                def read_thread():
                    with read:
                        setattr(self, attr, read.read())

                thread = ThreadWithReturn(read_thread)
                thread.start()
                self._running_threads.append(thread)
                self._open_files.append(write)
                self._enter_pipes[pipe_index] = write

    def __exit__(self, *args):
        # We have to close files before we join threads, because e.g. reader
        # threads won't receive EOF until we close the write end of their pipe.
        # TODO: Handle exceptions that might occur in here, like in closing a
        # file.
        for f in self._open_files:
            f.close()
        for t in self._running_threads:
            t.join()


def update_env(parent, env, full_env):
    '''We support the 'env' parameter to add environment variables to the
    default environment (this differs from subprocess's standard behavior, but
    it's by far the most common use case), and the 'full_env' parameter to
    supply the entire environment. Callers shouldn't supply both in one place,
    but it's possible for parameters on individual commands to edit or override
    what's given to run(). We also convert pathlib Paths to strings.'''
    if env is not None and full_env is not None:
        raise ValueError(
            'Cannot specify both env and full_env at the same time.')

    if parent is None:
        ret = os.environ.copy()
    else:
        ret = parent.copy()

    if env is not None:
        ret.update(env)
    if full_env is not None:
        ret = full_env

    # Support for pathlib Paths.
    ret = stringify_paths_in_dict(ret)

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
