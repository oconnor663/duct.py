#! /usr/bin/env python

from tubes import cmd, CheckedError

cmd('echo', 'hello', 'world').run()

print('output: "{}"'.format(cmd('echo', 'some     stuff').read()))

print('result:', cmd('echo', 'more stuff').result(stderr=True))

print('random:', cmd('head', '-c', '10', '/dev/urandom').read(bytes=True))

try:
    cmd('false').run()
except CheckedError as e:
    print('error:', e)

print('pipe:',
      cmd('cat', '/dev/zero')
      .pipe('head', '-c', '10')
      .pipe('cat', '-vet')
      .read(check=False))
