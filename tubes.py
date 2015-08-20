import collections
import subprocess


class Cmd:
    def __init__(self, prog, *args):
        self._cmd = (prog,) + args

    def result(self, check=True, strip=True, bytes=False, capture_stdout=True,
               capture_stderr=False):
        p = subprocess.Popen(
            self._cmd,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.PIPE if capture_stderr else None,
            universal_newlines=not bytes)
        stdout, stderr = p.communicate()
        if strip:
            stdout = stdout and stdout.strip()
            stderr = stderr and stderr.strip()
        result = Result(self._cmd, p.returncode, stdout, stderr)
        if check and p.returncode != 0:
            raise CheckedError(result)
        return result

    def run(self, capture_stdout=False, **kwargs):
        return self.result(capture_stdout=capture_stdout, **kwargs)

    def read(self, **kwargs):
        return self.result(**kwargs).stdout


Result = collections.namedtuple(
    'Result', ['args', 'returncode', 'stdout', 'stderr'])


class CheckedError(Exception):
    def __init__(self, result):
        self.result = result

    def __str__(self):
        return 'Command "{}" returned non-zero exit status {}'.format(
            ' '.join(self.result.args), self.result.returncode)
