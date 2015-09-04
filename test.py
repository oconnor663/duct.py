#! /usr/bin/env python

import os
os.environ['TROLLIUSDEBUG'] = '1'

from duct import cmd, cd, setenv, CheckedError

cmd('echo hello world').run()

out = cmd('echo', 'some     stuff').read()
print('output: "{}"'.format(out))

out = cmd('echo', 'more stuff').result(stderr=True)
print('result:', out)

out = cmd('head', '-c', '10', '/dev/urandom').read(bytes=True)
print('random:', out)

try:
    cmd('bash', '-c', 'exit 42').run()
except CheckedError as e:
    print('error:', e)

out = (cmd('cat', '/dev/zero')
       .pipe('head', '-c', '10')
       .pipe('cat', '-vet')
       .read(check=False))
print('pipe:', out)

out = (cmd('echo', '-n', 'hi')
       .then('false')
       .pipe('sed', 's/hi/hee/')
       .orthen('echo', 'haw')
       .read())
print('and/or:', out)

out = (cmd('echo moomoo')
       .pipe(cmd('head -c 3').pipe('sed s/o/a/g')
             .then('sed s/o/e/g'))
       .read())
print('nesting:', out)

out = cd('/tmp').then('pwd').read()
print('cd:', out)

out = setenv('MYVAR', 'foo').then('bash', '-c', 'echo "MYVAR=$MYVAR"').read()
print('setenv:', out)
