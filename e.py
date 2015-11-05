#! /usr/bin/env python3

import subprocess
import threading
import os
import time


def open_pipe():
    read_fd, write_fd = os.pipe()
    return os.fdopen(read_fd), os.fdopen(write_fd, 'w')

cat_code = '''\
import sys
while True:
    c = sys.stdin.read(1)
    if not c:
        break
    sys.stdout.write(c)
'''

cat_cmd = ['python', '-c', cat_code]

input_read, input_write = open_pipe()
pipe_read, pipe_write = open_pipe()
output_read, output_write = open_pipe()


# Write into the input pipe.
def write_input():
    with input_write:
        input_write.write("flimflam")
input_thread = threading.Thread(target=write_input)
input_thread.start()


# Read from the output pipe.
def read_output():
    global out
    with output_read:
        out = output_read.read()
output_thread = threading.Thread(target=read_output)
output_thread.start()


run_event = threading.Event()


def left():
    with input_read, pipe_write:
        run_event.wait()
        subprocess.call(cat_cmd, stdin=input_read, stdout=pipe_write)
left_thread = threading.Thread(target=left)
left_thread.start()


def right():
    with pipe_read, output_write:
        run_event.wait()
        subprocess.run(cat_cmd, stdin=pipe_read, stdout=output_write)
right_thread = threading.Thread(target=right)
right_thread.start()

time.sleep(0.1)
run_event.set()

right_thread.join()
left_thread.join()
input_thread.join()
output_thread.join()

# Read from the output pipe buffer.
print("got out:", out)
