import atexit
import collections
import os
import trollius
from trollius import From, Return


def cmd(*args, **kwargs):
    return Command(*args, **kwargs)


def _run_and_get_result(command, capture_stdout, capture_stderr, trim_mode,
                        bytes_mode, check_errors):
    # It's very unclear to me how to use the event loop properly for this. I'm
    # doing my best to refactor these nasty details into a separate function.
    def get_task(loop):
        return _run_with_pipes(loop, command, capture_stdout, capture_stderr)
    status, stdout_bytes, stderr_bytes = _run_async_task(get_task)
    if trim_mode:
        stdout_bytes = _trim_bytes_or_none(stdout_bytes)
        stderr_bytes = _trim_bytes_or_none(stderr_bytes)
    if not bytes_mode:
        stdout_bytes = _decode_bytes_or_none(stdout_bytes)
        stderr_bytes = _decode_bytes_or_none(stderr_bytes)
    result = Result(status, stdout_bytes, stderr_bytes)
    if check_errors and status != 0:
        raise CheckedError(result, command)
    return result


# It's completely unclear to me how to run our coroutines without disrupting
# asyncio callers outside this library. This funciton is the best I've got so
# far. Further questions:
#   https://redd.it/3id0fb
#   https://gist.github.com/oconnor663/f0ddad2c0bd1f7cf14c2
#   http://stackoverflow.com/q/32345735/823869
def _run_async_task(get_task):
    loop = trollius.get_event_loop()
    atexit.register(loop.close)
    task = get_task(loop)
    result = loop.run_until_complete(task)
    return result


@trollius.coroutine
def _run_with_pipes(loop, command, capture_stdout, capture_stderr):
    # The subprocess module acceps None for stdin/stdout/stderr, to mean leave
    # the default. We use that instead of hardcoding 0/1/2.
    stdout_write = None
    stderr_write = None
    # Create pipes if needed, and kick off reader coroutines.
    if capture_stdout:
        stdout_read, stdout_write, stdout_future = \
            yield From(_create_async_pipe(loop))
    if capture_stderr:
        stderr_read, stderr_write, stderr_future = \
            yield From(_create_async_pipe(loop))
    # Kick off the child processes.
    status = yield From(command._exec(loop, None, stdout_write, stderr_write))
    stdout_bytes = None
    stderr_bytes = None
    if capture_stdout:
        stdout_write.close()
        stdout_bytes = yield From(stdout_future)
        stdout_read.close()
    if capture_stderr:
        stderr_write.close()
        stderr_bytes = yield From(stderr_future)
        stderr_read.close()
    raise Return(status, stdout_bytes, stderr_bytes)


@trollius.coroutine
def _create_async_pipe(loop):
    # Create the pipe.
    read_fd, write_fd = os.pipe()
    read_pipe = os.fdopen(read_fd, 'rb')
    write_pipe = os.fdopen(write_fd, 'wb')
    # Hook it up to the event loop.
    stream_reader = trollius.StreamReader()
    yield From(loop.connect_read_pipe(
        lambda: trollius.StreamReaderProtocol(stream_reader),
        read_pipe))
    # Kick off a reader. If we gave this pipe to a child process without a
    # running reader, the child could fill up the pipe buffer and hang.
    read_future = loop.create_task(stream_reader.read())
    return read_pipe, write_pipe, read_future


class CommandBase:
    @trollius.coroutine
    def _exec(self, loop, stdin, stdout, stderr):
        raise NotImplementedError

    def result(self, check=True, trim=False, bytes=False, stdout=True,
               stderr=False):
        # Flags in the public API are given short names for convenience, but we
        # give them into more descriptive names internally.
        return _run_and_get_result(
            self, capture_stdout=stdout, capture_stderr=stderr, trim_mode=trim,
            bytes_mode=bytes, check_errors=check)

    def run(self, stdout=False, **kwargs):
        return self.result(stdout=stdout, **kwargs)

    def read(self, trim=True, **kwargs):
        return self.result(trim=trim, **kwargs).stdout

    def pipe(self, *args, **kwargs):
        return Pipe(self, cmd(*args, **kwargs))

    def then(self, *args, **kwargs):
        return Then(self, cmd(*args, **kwargs))

    def orthen(self, *args, **kwargs):
        return OrThen(self, cmd(*args, **kwargs))

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

    @trollius.coroutine
    def _exec(self, loop, stdin, stdout, stderr):
        p = yield From(trollius.subprocess.create_subprocess_exec(
            *self._tuple, loop=loop, stdin=stdin, stdout=stdout,
            stderr=stderr))
        status = yield From(p.wait())
        raise Return(status)

    def __repr__(self):
        return ' '.join(self._tuple)


class OperationBase(CommandBase):
    def __init__(self, left, right):
        self._left = left
        self._right = right


class Then(OperationBase):
    @trollius.coroutine
    def _exec(self, loop, stdin, stdout, stderr):
        # Execute the first command.
        status = yield From(self._left._exec(loop, stdin, stdout, stderr))
        # If it returns non-zero short-circuit.
        if status != 0:
            raise Return(status)
        # Otherwise execute the second command.
        status = yield From(self._right._exec(loop, stdin, stdout, stderr))
        raise Return(status)

    def __repr__(self):
        return repr(self._left) + ' && ' + repr(self._right)


class OrThen(OperationBase):
    @trollius.coroutine
    def _exec(self, loop, stdin, stdout, stderr):
        # Execute the first command.
        status = yield From(self._left._exec(loop, stdin, stdout, stderr))
        # If it returns zero short-circuit.
        if status == 0:
            raise Return(status)
        # Otherwise ignore the error and execute the second command.
        status = yield From(self._right._exec(loop, stdin, stdout, stderr))
        raise Return(status)

    def __repr__(self):
        return repr(self._left) + ' || ' + repr(self._right)


class Pipe(OperationBase):
    @trollius.coroutine
    def _exec(self, loop, stdin, stdout, stderr):
        # Open a read/write pipe. The write end gets passed to the left as
        # stdout, and the read end gets passed to the right as stdin. Either
        # side could be a compound expression (like A.then(B)), so we have to
        # wait until each command is completely finished before we can close
        # its end of the pipe. Closing the write end allows the right side to
        # receive EOF, and closing the read end allows the left side to receive
        # SIGPIPE.
        read_pipe, write_pipe = os.pipe()
        lfuture = loop.create_task(self._left._exec(
            loop, stdin, write_pipe, stderr))
        lfuture.add_done_callback(lambda f: os.close(write_pipe))
        rfuture = loop.create_task(self._right._exec(
            loop, read_pipe, stdout, stderr))
        rfuture.add_done_callback(lambda f: os.close(read_pipe))
        lstatus = yield From(lfuture)
        rstatus = yield From(rfuture)
        # Return the rightmost error, or zero if no errors.
        raise Return(lstatus if rstatus == 0 else rstatus)

    def __repr__(self):
        return repr(self._left) + ' | ' + repr(self._right)


Result = collections.namedtuple('Result', ['returncode', 'stdout', 'stderr'])


class CheckedError(Exception):
    def __init__(self, result, command):
        self.result = result
        self.command = command

    def __str__(self):
        return 'Command "{}" returned non-zero exit status {}'.format(
            self.command, self.result.returncode)


def _trim_bytes_or_none(b):
    newlines = b'\n\r'
    return None if b is None else b.rstrip(newlines)


def _decode_bytes_or_none(b):
    return None if b is None else b.decode()
