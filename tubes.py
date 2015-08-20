import collections
import subprocess


class Cmd:
    def __init__(self, prog, *args):
        self._cmd = (prog,) + args

    def _run(self, check, strip=True, bytes=False, **kwargs):
        p = subprocess.Popen(
            self._cmd,
            universal_newlines=not bytes,
            **kwargs)
        stdout, stderr = p.communicate()
        if strip:
            stdout = stdout and stdout.strip()
            stderr = stderr and stderr.strip()
        result = Result(self._cmd, p.returncode, stdout, stderr)
        if check and p.returncode != 0:
            raise CheckedError(result)
        return result

    def run(self, check=True):
        return self._run(check)

    def read(self, **kwargs):
        return self.result(**kwargs).stdout

    def result(self, check=True, capture_stderr=False, bytes=False,
               strip=True):
        stderr = subprocess.PIPE if capture_stderr else None
        return self._run(check, stdout=subprocess.PIPE, stderr=stderr)


Result = collections.namedtuple(
    'Result', ['args', 'returncode', 'stdout', 'stderr'])


class CheckedError(Exception):
    def __init__(self, result):
        self.result = result

    def __str__(self):
        return 'Command "{}" returned non-zero exit status {}'.format(
            ' '.join(self.result.args), self.result.returncode)
