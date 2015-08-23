#! /usr/bin/env python

from tubes import Cmd, CheckedError

Cmd('echo', 'hello', 'world').run()

print('output: "{}"'.format(Cmd('echo', 'some     stuff').read()))

print('result:', Cmd('echo', 'more stuff').result(stderr=True))

print('random:', Cmd('head', '-c', 10, '/dev/urandom').read(bytes=True))

try:
    Cmd('false').run()
except CheckedError as e:
    print('error:', e)

print('pipe:',
      Cmd('cat', '/dev/zero')
      .pipe('head', '-c', 10)
      .pipe('cat', '-vet')
      .read())
