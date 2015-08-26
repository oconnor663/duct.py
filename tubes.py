import atexit
import collections
import os
import subprocess
import trollius
from trollius import From, Return


def cmd(prog, *args):
    return Command(prog, *args)


class ExpressionBase:
    @trollius.coroutine
    def _exec(self, loop, stdin, stdout, stderr):
        raise NotImplementedError

    def result(self, check=True, trim=False, bytes=False, stdout=True,
               stderr=False):
        # It's very unclear to me how to use the event loop properly for this.
        # I'm doing my best to refactor these nasty details into a separate
        # function.
        def get_task(loop):
            return self._exec(
                loop=loop,
                stdin=None,
                stdout=subprocess.PIPE if stdout else None,
                stderr=subprocess.PIPE if stderr else None)
        result = _run_async_task(get_task)
        if trim:
            result = result.trim()
        if not bytes:
            result = result.decode()
        if check and result.returncode != 0:
            raise CheckedError(result, self)
        return result

    def run(self, stdout=False, **kwargs):
        return self.result(stdout=stdout, **kwargs)

    def read(self, trim=True, **kwargs):
        return self.result(trim=trim, **kwargs).stdout

    def pipe(self, prog, *args):
        rightside = cmd(prog, *args)
        return Pipe(self, rightside)

    def then(self, prog, *args):
        rightside = cmd(prog, *args)
        return Then(self, rightside)


class Command(ExpressionBase):
    def __init__(self, prog, *args):
        self._tuple = (prog,) + args

    @trollius.coroutine
    def _exec(self, loop, stdin, stdout, stderr):
        p = yield From(trollius.subprocess.create_subprocess_exec(
            *self._tuple, loop=loop, stdin=stdin, stdout=stdout,
            stderr=stderr))
        out, err = yield From(p.communicate())
        raise Return(Result(p.returncode, out, err))


class OperationBase(ExpressionBase):
    def __init__(self, left, right):
        self._left = left
        self._right = right


class Then(OperationBase):
    @trollius.coroutine
    def _exec(self, loop, stdin, stdout, stderr):
        # Execute the first expression.
        lresult = yield From(self._left._exec(loop, stdin, stdout, stderr))
        # If it returns non-zero short-circuit.
        if lresult.returncode != 0:
            raise Return(lresult)
        # Otherwise execute the second expression.
        rresult = yield From(self._right._exec(loop, stdin, stdout, stderr))
        raise Return(lresult.merge(rresult))


class Pipe(OperationBase):
    @trollius.coroutine
    def _exec(self, loop, stdin, stdout, stderr):
        # Open a read/write pipe. The write end gets passed to the left as
        # stdout, and the read end gets passed to the right as stdin. Either
        # side could be a compound expression (like A && B), so we have to wait
        # until each expression is completely finished before we can close its
        # end of the pipe. Closing the write end allows the right side to
        # receive EOF, and closing the read end allows the left side to receive
        # SIGPIPE.
        read_pipe, write_pipe = os.pipe()
        lfuture = loop.create_task(self._left._exec(
            loop, stdin, write_pipe, stderr))
        lfuture.add_done_callback(lambda f: os.close(write_pipe))
        rfuture = loop.create_task(self._right._exec(
            loop, read_pipe, stdout, stderr))
        rfuture.add_done_callback(lambda f: os.close(read_pipe))
        rresult = yield From(rfuture)
        lresult = yield From(lfuture)
        raise Return(lresult.merge(rresult))


_ResultBase = collections.namedtuple(
    '_ResultBase', ['returncode', 'stdout', 'stderr'])


class Result(_ResultBase):
    # When merging two results take the rightmost error code or zero and
    # concatenate both the outputs.
    def merge(self, second):
        returncode = second.returncode
        if returncode == 0:
            returncode = self.returncode
        return Result(returncode,
                      self._concat_or_none(self.stdout, second.stdout),
                      self._concat_or_none(self.stderr, second.stderr))

    @staticmethod
    def _concat_or_none(out1, out2):
        if out1 is None:
            return out2
        if out2 is None:
            return out1
        return out1 + out2

    def trim(self):
        return Result(self.returncode,
                      self._trim_or_none(self.stdout),
                      self._trim_or_none(self.stderr))

    @staticmethod
    def _trim_or_none(b):
        if b is None:
            return None
        newlines = b'\n\r'
        return b.rstrip(newlines)

    def decode(self):
        return Result(self.returncode,
                      self._decode_or_none(self.stdout),
                      self._decode_or_none(self.stderr))

    @staticmethod
    def _decode_or_none(b):
        if b is None:
            return None
        return b.decode()


class CheckedError(Exception):
    def __init__(self, result, expression):
        self.result = result
        self.expression = expression

    def __str__(self):
        return 'Command "{}" returned non-zero exit status {}'.format(
            self.expression, self.result.returncode)


# It's completely unclear to me how to run our coroutines without disrupting
# asyncio callers outside this library. This funciton is the best I've got so
# far. Further questions:
#   https://redd.it/3id0fb
#   https://gist.github.com/oconnor663/f0ddad2c0bd1f7cf14c2
def _run_async_task(get_task):
    loop = trollius.get_event_loop()
    atexit.register(loop.close)
    task = get_task(loop)
    result = loop.run_until_complete(task)
    return result
