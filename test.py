#! /usr/bin/env python

import os
os.environ['TROLLIUSDEBUG'] = '1'

from tubes import cmd, CheckedError

cmd('echo hello world').run()

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

print('and/or:',
      cmd('echo', '-n', 'hi')
      .then('false')
      .pipe('sed', 's/hi/hee/')
      .orthen('echo', 'haw')
      .read())
