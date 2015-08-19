import subprocess


class Cmd:
    def __init__(self, prog, *args):
        self.prog = prog
        self.args = args

    def run(self):
        subprocess.call((self.prog,) + self.args)
