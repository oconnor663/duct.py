#! /usr/bin/env python3

# This is a python version of this pipeline: `echo foo | cat`. It demonstrates
# a deadlock that can happen on Windows when Popen is called simultaneously
# from two different threads. Although os.pipe() file descriptors are not
# normally inheritable (never on Windows, and not in recent Python versions on
# POSIX), the Windows implementation of Popen() creates temporary inheritable
# copies of its descriptors. See _make_inheritable() in subprocess.py. If a
# second process is spawned while those inheritable copies are open, both
# children will inherit the copies. With pipes, this can cause deadlocks when
# extra open write handles keep a reader from ever getting EOF. See also:
# https://www.python.org/dev/peps/pep-0446/#security-vulnerability
#
# Demonstrating the deadlock depends on timing, which seems to vary a lot
# between different versions of Windows. The simplest way is to make a local
# copy of subprocess.py (here I call it subprocess_sleepy.py) and add short
# sleeps immediately before and after the call to CreateProcess():
# https://gist.github.com/oconnor663/b1d39d58b232fc627d84

from subprocess_sleepy import Popen, PIPE
import threading
import os

pipe_read, pipe_write = os.pipe()


# Launch the left half of the pipe in a separate thread.
def start_echo_foo():
    echo_foo_cmd = ['python', '-c', 'print("foo")']
    proc = Popen(echo_foo_cmd, stdout=pipe_write)
    proc.wait()
    os.close(pipe_write)
thread = threading.Thread(target=start_echo_foo)
thread.start()


# Launch the right half of the pipe here in the main thread.
cat_cmd = ['python', '-c', 'import sys; sys.stdout.write(sys.stdin.read())']
proc = Popen(cat_cmd, stdin=pipe_read, stdout=PIPE)
out, _ = proc.communicate()  # <--- This can deadlock on Windows!
os.close(pipe_read)

thread.join()

# Read from the output pipe buffer.
print("got out:", out)
