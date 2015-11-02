#! /usr/bin/env python3

import subprocess
import threading
import os

cat = '''
import sys
import time
time.sleep(0.5)
while True:
    c = sys.stdin.read(1)
    if not c:
        break
    sys.stdout.write(c)
'''

cat_cmd = ['python', '-c', cat]

read_pipe, write_pipe = os.pipe()


def left():
    print("starting left cat...")
    subprocess.run(cat_cmd, input=b"foo", stdout=write_pipe)
    print("finished left cat, closing left end of the pipe...")
    os.close(write_pipe)
    print("left end closed.")
left_thread = threading.Thread(target=left)
left_thread.start()

print('starting right cat...')
out = subprocess.run(cat_cmd, stdin=read_pipe, stdout=subprocess.PIPE)
print('finished right cat, closing right end of the pipe...')
os.close(read_pipe)
print('right end closed. got:', repr(out.stdout))
