import collections
import os
import subprocess
import trollius
from trollius import From, Return


class ExpressionBase:
    @trollius.coroutine
    def _exec(self, stdin, stdout, stderr):
        raise NotImplementedError

    def result(self):
        loop = trollius.get_event_loop()
        return loop.run_until_complete(self._exec(
            stdin=None, stdout=subprocess.PIPE, stderr=None))


class Command(ExpressionBase):
    def __init__(self, prog, *args):
        self._tuple = (prog,) + args

    @trollius.coroutine
    def _exec(self, stdin, stdout, stderr):
        p = yield From(trollius.subprocess.create_subprocess_exec(
            *self._tuple, stdin=stdin, stdout=stdout, stderr=stderr))
        out, err = yield From(p.communicate())
        raise Return(Result(p.returncode, out, err))


class OperationBase(ExpressionBase):
    def __init__(self, left, right):
        self._left = left
        self._right = right


class And(OperationBase):
    @trollius.coroutine
    def _exec(self, stdin, stdout, stderr):
        # Execute the first expression.
        lresult = yield From(self._left._exec(stdin, stdout, stderr))
        # If it returns non-zero short-circuit.
        if lresult.returncode != 0:
            raise Return(lresult)
        # Otherwise execute the second expression.
        rresult = yield From(self._right._exec(stdin, stdout, stderr))
        raise Return(lresult.merge(rresult))


class Pipe(OperationBase):
    @trollius.coroutine
    def _exec(self, stdin, stdout, stderr):
        # Open a read/write pipe.
        read_pipe, write_pipe = os.pipe()
        # Execute both expressions in parallel, connected by the pipe.
        lfuture = self._left._exec(stdin, write_pipe, stderr)
        rfuture = self._right._exec(read_pipe, stdout, stderr)
        lresult, rresult = yield From(trollius.gather(lfuture, rfuture))
        # Return the rightmost error, if any.
        rightmosterror = rresult.returncode
        if rightmosterror == 0:
            rightmosterror = lresult.returncode
        ret = lresult.merge(rresult)._replace(returncode=rightmosterror)
        raise Return(ret)


class Cmd:
    def __init__(self, prog, *args):
        self._pipeline = []
        self.pipe(prog, *args)

    def pipe(self, prog, *args):
        # TODO: Be somewhat stricter about types here.
        cmd = tuple(str(i) for i in (prog,) + args)
        self._pipeline.append(cmd)
        return self

    def result(self, check=True, trim=False, bytes=False, stdout=True,
               stderr=False):
        last_proc = None
        # Kick off all but the final pipelined command.
        for cmd in self._pipeline[:-1]:
            this_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stdin=last_proc and last_proc.stdout)
            # Allow last_proc to receive SIGPIPE.
            last_proc and last_proc.stdout.close()
            last_proc = this_proc
        # Kick off the final command, respecting output options.
        p = subprocess.Popen(
            self._pipeline[-1],
            stdin=last_proc.stdout if last_proc else None,
            stdout=subprocess.PIPE if stdout else None,
            stderr=subprocess.PIPE if stderr else None,
            universal_newlines=not bytes)
        # Allow last_proc to receive SIGPIPE. TODO: Deduplicate this.
        last_proc and last_proc.stdout.close()
        stdout, stderr = p.communicate()
        if trim:
            newlines = b'\n\r' if bytes else '\n\r'
            stdout = stdout and stdout.rstrip(newlines)
            stderr = stderr and stderr.rstrip(newlines)
        result = Result(p.returncode, stdout, stderr)
        if check and p.returncode != 0:
            raise CheckedError(result, self._pipeline)
        return result

    def run(self, stdout=False, **kwargs):
        return self.result(stdout=stdout, **kwargs)

    def read(self, trim=True, **kwargs):
        return self.result(trim=trim, **kwargs).stdout


_ResultBase = collections.namedtuple(
    '_ResultBase', ['returncode', 'stdout', 'stderr'])


class Result(_ResultBase):
    # When merging two results (for example, A && B), take the second return
    # code and concatenate both the outputs.
    def merge(self, second):
        return Result(second.returncode,
                      self._concat(self.stdout, second.stdout),
                      self._concat(self.stderr, second.stderr))

    @staticmethod
    def _concat(out1, out2):
        if out1 is None:
            return out2
        if out2 is None:
            return out1
        return out1 + out2


class CheckedError(Exception):
    def __init__(self, result, pipeline):
        self.result = result
        self.pipeline = pipeline

    def __str__(self):
        return 'Command "{}" returned non-zero exit status {}'.format(
            format_pipe(self.pipeline), self.result.returncode)


def format_pipe(pipeline):
    return ' | '.join(' '.join(command) for command in pipeline)
