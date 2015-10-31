#! /usr/bin/env python3

import os
from subprocess import Popen
from threading import Thread

cat = ['python', '-c', 'import sys; sys.stdout.write(sys.stdin.read())']

inputr, inputw = os.pipe()
piper, pipew = os.pipe()
outputr, outputw = os.pipe()
Popen(cat, stdin=inputr, stdout=pipew)
Popen(cat, stdin=piper, stdout=outputw)
os.close(inputr)
os.close(piper)
os.close(pipew)
os.close(outputw)


def write():
    with open(inputw, 'wb') as inputf:
        inputf.write(b"foo")
writer = Thread(target=write)
print("spawning writer.")
writer.start()

print("reading.")
with open(outputr, 'rb') as outputf:
    stdout = outputf.read()
print("got:", stdout)

print("joining writer.")
writer.join()
