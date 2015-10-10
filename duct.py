import collections
import os
import pathlib
import re
import subprocess
import threading

# Public API
# ==========


def cmd(*parts):
    return Command(*parts)


def sh(s):
    return cmd(*split_string_with_quotes(s))


def cd(path):
    return Cd(path)


def setenv(name, val):
    return SetEnv(name, val)


class AbstractExpression:
    def run(self, stdout=None, **result_kwargs):
        return self.result(stdout=stdout, **result_kwargs)

    def read(self, **result_kwargs):
        result = self.result(**result_kwargs)
        return result.stdout

    def result(self, stdin=None, stdout=str, stderr=None, check=True,
               trim=True):
        return execute_expression(self, stdin, stdout, stderr, trim, check)

    def pipe(self, *cmd):
        return Pipe(self, command_or_parts(*cmd))

    def then(self, *cmd):
        return Then(self, command_or_parts(*cmd))

    def orthen(self, *cmd):
        return OrThen(self, command_or_parts(*cmd))


# Implementation Details
# ======================


# Set up any readers or writers, kick off the recurisve _exec(), and collect
# the results. This is the core of the three execution methods: run(), read(),
# and result().
def execute_expression(expr, stdin, stdout, stderr, trim, check):
    stdin_writer = InputWriter(stdin)
    stdout_reader = OutputReader(stdout)
    stderr_reader = OutputReader(stderr)
    with stdin_writer as stdin_pipe, \
            stdout_reader as stdout_pipe, \
            stderr_reader as stderr_pipe:
        # Kick off the child processes. We discard the cwd and env returns.
        status, _, _ = expr._exec(
            stdin_pipe, stdout_pipe, stderr_pipe, None, None)
    stdout_output = stdout_reader.get_output()
    stderr_output = stderr_reader.get_output()
    if trim:
        stdout_output = _trim_if_string(stdout_output)
        stderr_output = _trim_if_string(stderr_output)
    result = Result(status, stdout_output, stderr_output)
    if check and status != 0:
        raise CheckedError(result, expr)
    return result


# Methods like pipe() take a command argument. This can either be arguments to
# a Command constructor, or it can be an already-fully-formed command, like
# another compount expression or the output of sh().
def command_or_parts(first, *rest):
    if isinstance(first, AbstractExpression):
        if rest:
            raise TypeError("When an expression object is given, "
                            "no other arguments are allowed.")
        return first
    return Command(first, *rest)


class Command(AbstractExpression):
    def __init__(self, prog, *args):
        '''The prog and args will be passed directly to subprocess.call(),
        which determines the types allowed here (strings and bytes). In
        addition, we also explicitly support pathlib Paths, by converting them
        to strings.'''
        converted_parts = []
        for part in (prog,) + args:
            if isinstance(part, pathlib.PurePath):
                converted_parts.append(str(part))
            else:
                converted_parts.append(part)
        self._tuple = tuple(converted_parts)

    def _exec(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd, env):
        full_env = None
        # The env parameter only contains additional env vars. We need to copy
        # the entire working environment first if we're going to pass it in.
        if env is not None:
            full_env = os.environ.copy()
            full_env.update(env)
        status = subprocess.call(
            self._tuple, stdin=stdin_pipe, stdout=stdout_pipe,
            stderr=stderr_pipe, cwd=cwd, env=full_env)
        # A normal command never changes cwd or env.
        return CommandExit(status, cwd, env)

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


class Cd(AbstractExpression):
    def __init__(self, path):
        # Stringifying the path lets us support pathlib.Path's here.
        self._path = str(path)

    def _exec(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd, env):
        # Check that the path is legit.
        if not os.path.isdir(self._path):
            raise ValueError(
                '"{}" is not a valid directory.'.format(self._path))
        # Return it so that subsequent commands will use it as the cwd.
        return CommandExit(0, self._path, env)

    def __repr__(self):
        return 'cd ' + self._path


class SetEnv(AbstractExpression):
    def __init__(self, name, val):
        self._name = name
        self._val = val

    def _exec(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd, env):
        # TODO: Support deletions and dictionary arguments.
        new_env = env.copy() if env is not None else {}
        new_env[self._name] = self._val
        return CommandExit(0, cwd, new_env)

    def __repr__(self):
        return 'setenv {} {}'.format(self._name, self._val)


class CompoundExpression(AbstractExpression):
    def __init__(self, left, right):
        self._left = left
        self._right = right


class Then(CompoundExpression):
    def _exec(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd, env):
        # Execute the first command.
        left_exit = self._left._exec(
            stdin_pipe, stdout_pipe, stderr_pipe, cwd, env)
        # If it returns non-zero short-circuit.
        if left_exit.status != 0:
            return left_exit
        # Otherwise execute the second command.
        right_exit = self._right._exec(
            stdin_pipe, stdout_pipe, stderr_pipe, left_exit.cwd, left_exit.env)
        return right_exit

    def __repr__(self):
        return join_with_maybe_parens(self._left, self._right, ' && ', Pipe)


class OrThen(CompoundExpression):
    def _exec(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd, env):
        # Execute the first command.
        left_exit = self._left._exec(
            stdin_pipe, stdout_pipe, stderr_pipe, cwd, env)
        # If it returns zero short-circuit.
        if left_exit.status == 0:
            return left_exit
        # Otherwise ignore the error and execute the second command.
        right_exit = self._right._exec(
            stdin_pipe, stdout_pipe, stderr_pipe, left_exit.cwd, left_exit.env)
        return right_exit

    def __repr__(self):
        return join_with_maybe_parens(self._left, self._right, ' || ', Pipe)


# Pipe uses another thread to run the left side of the pipe in parallel with
# the right. This is required because both the left and the right might be
# compound expressions, where a second command might need to be started after
# the first finishes, so someone has to be waiting on both sides at the same
# time. There are Unix-specific ways to wait on multiple processes at once, but
# those can conflict with other listeners that might by in the same process,
# and they won't work on Windows anyway.
class Pipe(CompoundExpression):
    def _exec(self, stdin_pipe, stdout_pipe, stderr_pipe, cwd, env):
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
                return self._left._exec(
                    stdin_pipe, write_pipe, stderr_pipe, cwd, env)
        left_thread = ThreadWithReturn(target=do_left)
        left_thread.start()

        with read_pipe:
            right_exit = self._right._exec(
                read_pipe, stdout_pipe, stderr_pipe, cwd, env)
        left_exit = left_thread.join()

        # Return the rightmost error, if any. Note that cwd and env changes
        # never propagate out of the pipe. This is the same behavior as bash.
        if right_exit.status != 0:
            return CommandExit(right_exit.status, cwd, env)
        else:
            return CommandExit(left_exit.status, cwd, env)

    def __repr__(self):
        return join_with_maybe_parens(
            self._left, self._right, ' | ', (Then, OrThen))


CommandExit = collections.namedtuple('CommandExit', ['status', 'cwd', 'env'])

Result = collections.namedtuple('Result', ['status', 'stdout', 'stderr'])


class CheckedError(Exception):
    def __init__(self, result, command):
        self.result = result
        self.command = command

    def __str__(self):
        return 'Command "{}" returned non-zero exit status {}.'.format(
            self.command, self.result.status)


def _trim_if_string(x):
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


def _open_pipe(binary_mode):
    read_fd, write_fd = os.pipe()
    read_mode, write_mode = ('rb', 'wb') if binary_mode else ('r', 'w')
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


class OutputReader:
    def __init__(self, arg):
        '''This class handles the user's stdout or stderr argument. It produces
        a file/fileno to use with subprocess.call(), kicks off reader threads
        (if appropriate), and collects output (if appropriate).'''
        self._arg = arg
        self._output = None
        self._read = None
        self._write = None
        self._thread = None

    def __enter__(self):
        if self._arg is str or self._arg is bytes:
            # The caller passed the str or bytes type (e.g. stdout=str).
            # Collect output into the corresponding type.
            binary_mode = self._arg is bytes
            self._read, self._write = _open_pipe(binary_mode)
            self._thread = ThreadWithReturn(self._read.read)
            self._thread.start()
            return self._write
        else:
            # Otherwise assume the argument is suitable for subprocess.call().
            # (That is, it should either be a file descriptor or a file object
            # backed by a file descriptor, or None to share the parent's
            # stdout/stderr.)
            return self._arg

    def __exit__(self, *args):
        # Control resumes here when child processes are finished. If we opened
        # a pipe or spawned any threads in __enter__, we need to collect output
        # and clean up.
        if self._write:
            self._write.close()
            self._output = self._thread.join()
            self._read.close()
        # Allow exceptions to propagate.
        return False

    def get_output(self):
        return self._output


class InputWriter:
    def __init__(self, arg):
        '''This class handles the user's stdin argument. It produces a
        file/fileno to use with subprocess.call() and kicks off a writer thread
        (if appropriate).'''
        self._arg = arg
        self._output = None
        self._read = None
        self._write = None
        self._thread = None

    def __enter__(self):
        if isinstance(self._arg, str) or isinstance(self._arg, bytes):
            # The caller passed a string or bytes object (e.g. stdin="foo").
            # Use it as input.
            if not self._arg:
                # Avoid spawning a thread for empty input.
                return subprocess.DEVNULL
            binary_mode = isinstance(self._arg, bytes)
            self._read, self._write = _open_pipe(binary_mode)

            # The writer thread must close the write end of the pipe itself, or
            # child processes that read stdin will hang waiting for EOF.
            def write_and_close():
                self._write.write(self._arg)
                self._write.close()
            self._thread = ThreadWithReturn(write_and_close)
            self._thread.start()
            return self._read
        else:
            # Otherwise assume the argument is suitable for subprocess.call().
            # (That is, it should either be a file descriptor or a file object
            # backed by a file descriptor, or None to share the parent's
            # stdin.)
            return self._arg

    def __exit__(self, *args):
        # Control resumes here when child processes are finished. If we opened
        # a pipe or spawned any threads in __enter__, we need to collect output
        # and clean up.
        if self._write:
            self._read.close()
            self._thread.join()
        # Allow exceptions to propagate.
        return False


def split_string_with_quotes(s):
    words = []
    word = ""
    quote_starters = ("'", '"')
    starter = None
    for c in s:
        # Are we in a quotation?
        if starter is not None:
            # Is this the end of the quotation?
            if c == starter:
                starter = None
                # Note that we don't necessarily finish the word.
            # The quotation's still going.
            else:
                word += c
        # Are we starting a quotation?
        elif c in quote_starters:
            starter = c
        # We're not in a quotation. Are we done with a word?
        elif c.isspace():
            # Add the finished word, if we were in the middle of one.
            if word:
                words.append(word)
                word = ""
        # We're in the middle of a word.
        else:
            word += c
    # Make sure we didn't end with an unclosed quote.
    if starter is not None:
        raise SplitError("unclosed quote in: " + repr(s))
    # Finally, add the last word if the string ended in the middle of one.
    if word:
        words.append(word)
    return words


class SplitError(Exception):
    pass
