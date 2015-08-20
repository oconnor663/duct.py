#! /usr/bin/env python

from tubes import Cmd, CheckedError

Cmd('echo', 'hello', 'world').run()

print('output: "{}"'.format(Cmd('echo', 'some     stuff').read()))

print('result:', Cmd('true').result(stderr=True))

try:
    Cmd('false').run()
except CheckedError as e:
    print('error:', e)
