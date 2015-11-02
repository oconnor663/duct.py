#! /usr/bin/env python3

import subprocess
import threading
import os
import time

cat = '''
import sys
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
    left_proc = subprocess.Popen(
        cat_cmd, stdin=subprocess.PIPE, stdout=write_pipe)
    time.sleep(0.5)
    print("communicating with left cat...")
    left_proc.communicate(input=b"foo")
    print("finished left cat, closing left end of the pipe...")
    os.close(write_pipe)
    print("left end closed.")
left_thread = threading.Thread(target=left)
left_thread.start()

print('starting right cat...')
right_proc = subprocess.Popen(cat_cmd, stdin=read_pipe, stdout=subprocess.PIPE)
time.sleep(0.5)
print('communicating with right cat.')
out = right_proc.communicate()
print('finished right cat, closing right end of the pipe...')
os.close(read_pipe)
print('right end closed. got:', repr(out))
