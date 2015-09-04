import collections
import contextlib
import os
import subprocess
import threading


def cmd(*args, **kwargs):
    return Command(*args, **kwargs)


def cd(*args, **kwargs):
    return Cd(*args, **kwargs)


def setenv(*args, **kwargs):
    return SetEnv(*args, **kwargs)


def _run_and_get_result(command, capture_stdout, capture_stderr, trim_mode,
                        binary_mode, check_errors):
    status, output, err_output = _run_with_pipes(
        command, capture_stdout, capture_stderr, binary_mode)
    if trim_mode:
        output = _trim_or_none(output)
        err_output = _trim_or_none(err_output)
    result = Result(status, output, err_output)
    if check_errors and status != 0:
        raise CheckedError(result, command)
    return result


def _run_with_pipes(command, capture_stdout, capture_stderr, binary_mode):
    # The subprocess module acceps None for stdin/stdout/stderr, to mean "leave
    # the default". We use that instead of hardcoding 0/1/2.
    stdout_write = context_manager_giving_none()
    stderr_write = context_manager_giving_none()
    # Create pipes if needed, and kick off reader threads. Note that if we ran
    # the child process without reader threads, it could fill up its pipe
    # buffers and hang.
    if capture_stdout:
        stdout_read, stdout_write = _open_pipe(binary_mode)
        stdout_thread = ThreadWithReturn(stdout_read.read)
        stdout_thread.start()
    if capture_stderr:
        stderr_read, stderr_write = _open_pipe(binary_mode)
        stderr_thread = ThreadWithReturn(stderr_read.read)
        stderr_thread.start()
    with stdout_write as stdout, stderr_write as stderr:
        # Kick off the child processes. We discard the cwd and env values.
        status, _, _ = command._exec(None, stdout, stderr, None, None)
        stdout_bytes = None
        stderr_bytes = None
        if capture_stdout:
            # This close has to happen before join() or else the reader threads
            # will never finish. It should be safe even though the with block
            # is also going to close it.
            stdout_write.close()
            stdout_bytes = stdout_thread.join()
        if capture_stderr:
            # Same as above.
            stderr_write.close()
            stderr_bytes = stderr_thread.join()
        return Result(status, stdout_bytes, stderr_bytes)


def _new_or_existing_command(first, *rest, **kwargs):
    # If the arguments are strings, parse them the normal way.
    if isinstance(first, str):
        return Command(first, *rest, **kwargs)
    # Otherwise, the arguments must be a single command object.
    if not isinstance(first, CommandBase):
        raise TypeError("First argument must be a string or a command object.")
    if rest or kwargs:
        raise TypeError("When a command object is given, "
                        "no other arguments are allowed.")
    return first


class CommandBase:
    def _exec(self, stdin, stdout, stderr, cwd, env):
        raise NotImplementedError

    def result(self, check=True, trim=False, bytes=False, stdout=True,
               stderr=False):
        # Flags in the public API are given short names for convenience, but we
        # give them into more descriptive names internally.
        return _run_and_get_result(
            self, capture_stdout=stdout, capture_stderr=stderr, trim_mode=trim,
            binary_mode=bytes, check_errors=check)

    def run(self, stdout=False, **kwargs):
        return self.result(stdout=stdout, **kwargs)

    def read(self, trim=True, **kwargs):
        return self.result(trim=trim, **kwargs).stdout

    def pipe(self, *args, **kwargs):
        return Pipe(self, _new_or_existing_command(*args, **kwargs))

    def then(self, *args, **kwargs):
        return Then(self, _new_or_existing_command(*args, **kwargs))

    def orthen(self, *args, **kwargs):
        return OrThen(self, _new_or_existing_command(*args, **kwargs))

    def __repr__(self):
        raise NotImplementedError


class Command(CommandBase):
    def __init__(self, prog, *args):
        # If no explicit arguments are provided, split the program string on
        # whitespace and interpret any separate words as args. This allows the
        # user to type a command like "cat -vet /dev/urandom" as a single
        # string instead of typing [","] between each word.
        # XXX: This makes it impossible to directly invoke a program named
        # "with space" if there aren't any positional arguments. But...does
        # that ever happen?
        if not args:
            self._tuple = prog.split()
        else:
            self._tuple = (prog,) + args

    def _exec(self, stdin, stdout, stderr, cwd, env):
        full_env = None
        # The env parameter only contains additional env vars. We need to copy
        # the entire working environment first if we're going to pass it in.
        if env is not None:
            full_env = os.environ.copy()
            full_env.update(env)
        status = subprocess.call(
            self._tuple, stdin=stdin, stdout=stdout, stderr=stderr, cwd=cwd,
            env=full_env)
        # A normal command never changes cwd or env.
        return CommandExit(status, cwd, env)

    def __repr__(self):
        return ' '.join(self._tuple)


class Cd(CommandBase):
    def __init__(self, path):
        # Stringifying the path lets us support pathlib.Path's here.
        self._path = str(path)

    def _exec(self, stdin, stdout, stderr, cwd, env):
        # Check that the path is legit.
        if not os.path.isdir(self._path):
            raise ValueError(
                '"{}" is not a valid directory.'.format(self._path))
        # Return it so that subsequent commands will use it as the cwd.
        return CommandExit(0, self._path, env)

    def __repr__(self):
        return 'cd ' + self._path


class SetEnv(CommandBase):
    def __init__(self, name, val):
        self._name = name
        self._val = val

    def _exec(self, stdin, stdout, stderr, cwd, env):
        # TODO: Support deletions and dictionary arguments.
        new_env = env.copy() if env is not None else {}
        new_env[self._name] = self._val
        return CommandExit(0, cwd, new_env)

    def __repr__(self):
        return 'setenv {} {}'.format(self._name, self._val)


class OperationBase(CommandBase):
    def __init__(self, left, right):
        self._left = left
        self._right = right


class Then(OperationBase):
    def _exec(self, stdin, stdout, stderr, cwd, env):
        # Execute the first command.
        left_exit = self._left._exec(stdin, stdout, stderr, cwd, env)
        # If it returns non-zero short-circuit.
        if left_exit.status != 0:
            return left_exit
        # Otherwise execute the second command.
        right_exit = self._right._exec(
            stdin, stdout, stderr, left_exit.cwd, left_exit.env)
        return right_exit

    def __repr__(self):
        return repr(self._left) + ' && ' + repr(self._right)


class OrThen(OperationBase):
    def _exec(self, stdin, stdout, stderr, cwd, env):
        # Execute the first command.
        left_exit = self._left._exec(stdin, stdout, stderr, cwd, env)
        # If it returns zero short-circuit.
        if left_exit.status == 0:
            return left_exit
        # Otherwise ignore the error and execute the second command.
        right_exit = self._right._exec(
            stdin, stdout, stderr, left_exit.cwd, left_exit.env)
        return right_exit

    def __repr__(self):
        return repr(self._left) + ' || ' + repr(self._right)


# Pipe uses another thread to run the left side of the pipe in parallel with
# the right. This is required because both the left and the right might be
# compound expressions, where a second command might need to be started after
# the first finishes, so someone has to be waiting on both sides at the same
# time. There are Unix-specific ways to wait on multiple processes at once, but
# those can conflict with other listeners that might by in the same process,
# and they won't work on Windows anyway.
class Pipe(OperationBase):
    def _exec(self, stdin, stdout, stderr, cwd, env):
        # Open a read/write pipe. The write end gets passed to the left as
        # stdout, and the read end gets passed to the right as stdin. Either
        # side could be a compound expression (like A.then(B)), so we have to
        # wait until each command is completely finished before we can close
        # its end of the pipe. Closing the write end allows the right side to
        # receive EOF, and closing the read end allows the left side to receive
        # SIGPIPE.
        read_pipe, write_pipe = _open_pipe(binary_mode=True)

        def do_left():
            with write_pipe:
                return self._left._exec(stdin, write_pipe, stderr, cwd, env)
        left_thread = ThreadWithReturn(target=do_left)
        left_thread.start()

        with read_pipe:
            right_exit = self._right._exec(read_pipe, stdout, stderr, cwd, env)
        left_exit = left_thread.join()

        # Return the rightmost error, if any. Note that cwd and env changes
        # never propagate out of the pipe. This is the same behavior as bash.
        if right_exit.status != 0:
            return CommandExit(right_exit.status, None, None)
        else:
            return CommandExit(left_exit.status, None, None)

    def __repr__(self):
        return repr(self._left) + ' | ' + repr(self._right)


CommandExit = collections.namedtuple('CommandExit', ['status', 'cwd', 'env'])

Result = collections.namedtuple('Result', ['status', 'stdout', 'stderr'])


class CheckedError(Exception):
    def __init__(self, result, command):
        self.result = result
        self.command = command

    def __str__(self):
        return 'Command "{}" returned non-zero exit status {}.'.format(
            self.command, self.result.status)


def _trim_or_none(x):
    newlines = b'\n\r' if isinstance(x, bytes) else '\n\r'
    return None if x is None else x.rstrip(newlines)


def _open_pipe(binary_mode):
    read_fd, write_fd = os.pipe()
    read_mode, write_mode = ('rb', 'wb') if binary_mode else ('r', 'w')
    return os.fdopen(read_fd, read_mode), os.fdopen(write_fd, write_mode)


@contextlib.contextmanager
def context_manager_giving_none():
    yield None


class ThreadWithReturn(threading.Thread):
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
