#! /usr/bin/env python3

import os
from subprocess import Popen, PIPE


cat = ['python', '-c', 'import sys; sys.stdout.write(sys.stdin.read())']


def run(*, close):
    print("running {} close".format("WITH" if close else "WITHOUT"))
    r, w = os.pipe()
    left = Popen(cat, stdin=PIPE, stdout=w)
    right = Popen(cat, stdin=r, stdout=PIPE)
    if close:
        os.close(r)
        os.close(w)
    print("writing")
    left.communicate(b"foo")
    print("wrote. waiting.")
    stdout, stder = right.communicate()
    print("got:", stdout)


run(close=True)
print()
run(close=False)
