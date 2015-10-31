#! /usr/bin/env python3

import os
from subprocess import Popen

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
print("writing")
os.write(inputw, b"foo")
os.close(inputw)
print("wrote. waiting.")
stdout = os.read(outputr, 100)
os.close(outputr)
print("got:", stdout)
