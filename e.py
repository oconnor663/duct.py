#! /usr/bin/env python3

import subprocess
import threading
import os


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

# Write into the input pipe buffer.
with input_write:
    input_write.write("foooopsadfpasodif")


def left():
    with input_read, pipe_write:
        print("starting left cat...")
        subprocess.call(cat_cmd, stdin=input_read, stdout=pipe_write)
        print("finished left cat, closing left pipes...")
    print("left pipes closed.")
left_thread = threading.Thread(target=left)
left_thread.start()

with pipe_read, output_write:
    print('starting right cat...')
    subprocess.run(cat_cmd, stdin=pipe_read, stdout=output_write)
    print('finished right cat, closing right pipes...')
print('right pipes closed.')

left_thread.join()

# Read from the output pipe buffer.
with output_read:
    out = output_read.read()
print("got out:", out)
