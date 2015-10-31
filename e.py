#! /usr/bin/env python3

from duct import cmd
import textwrap


def cat(arg):
    code = textwrap.dedent('''\
        import os
        import sys
        import random
        import time
        time.sleep(random.random())
        print('starting.', '{0}', file=sys.stderr)
        for i in range(10):
            try:
                print(i, os.stat(i) and True, file=sys.stderr)
            except OSError:
                pass
        time.sleep(random.random())
        print('reading.', '{0}', file=sys.stderr)
        input = sys.stdin.read()
        time.sleep(random.random())
        print('read finished. writing.', '{0}', file=sys.stderr)
        sys.stdout.write(input)
        time.sleep(random.random())
        print('write finished.', '{0}', file=sys.stderr)
        '''.format(arg))
    return cmd('python', '-c', code)


print("starting pipe")
out = cat('left').pipe(cat('right')).read(input="second")
print("got", repr(out))
