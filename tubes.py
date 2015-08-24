import collections
import subprocess


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
