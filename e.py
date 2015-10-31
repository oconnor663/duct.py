#! /usr/bin/env python3

from duct import cmd
import textwrap


def cat():
    code = textwrap.dedent('''\
        import sys
        input = sys.stdin.read()
        sys.stdout.write(input)
        ''')
    return cmd('python', '-c', code)


print("starting replace")
out = cat().read(input='first')
print("got", repr(out))
print("starting pipe")
out = cat().pipe(cat()).read(input="second")
print("got", repr(out))
