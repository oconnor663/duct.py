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

    def result(self, check=True, strip=True, bytes=False, stdout=True,
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
        if strip:
            stdout = stdout and stdout.strip()
            stderr = stderr and stderr.strip()
        result = Result(p.returncode, stdout, stderr)
        if check and p.returncode != 0:
            raise CheckedError(result, self._pipeline)
        return result

    def run(self, stdout=False, **kwargs):
        return self.result(stdout=stdout, **kwargs)

    def read(self, **kwargs):
        return self.result(**kwargs).stdout


Result = collections.namedtuple(
    'Result', ['returncode', 'stdout', 'stderr'])


class CheckedError(Exception):
    def __init__(self, result, pipeline):
        self.result = result
        self.pipeline = pipeline

    def __str__(self):
        return 'Command "{}" returned non-zero exit status {}'.format(
            format_pipe(self.pipeline), self.result.returncode)


def format_pipe(pipeline):
    return ' | '.join(' '.join(command) for command in pipeline)
