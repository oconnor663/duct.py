#! /usr/bin/env python

from duct import cmd, sh, CheckedError
from pathlib import Path

sh('echo hello world').run()

out = sh('echo "some     stuff"').read()
print('output: "{}"'.format(out))

out = sh('echo more stuff').run(stdout=str)
print('result:', out)

out = sh('head -c 10 /dev/urandom').read(stdout=bytes)
print('random:', out)

try:
    cmd('bash', '-c', 'exit 42').run()
except CheckedError as e:
    print('error:', e)

out = (sh('cat /dev/zero')
       .pipe(sh('head -c 10'))
       .pipe('cat', '-vet')
       .read(check=False))
print('pipe:', out)

out = (sh('echo -n hi')
       .then('false', check=False)
       .pipe('sed', 's/hi/hee/')
       .then('echo', 'haw')
       .read())
print('and/or:', out)

out = (sh('echo moomoo')
       .pipe(sh('head -c 3').pipe('sed', 's/o/a/g')
             .then('sed', 's/o/e/g'))
       .read())
print('nesting:', out)

out = cmd('pwd').read(cwd=Path('/tmp'))
print('cd:', out)

out = cmd('bash', '-c', 'echo "MYVAR=\'$MYVAR\'"').read(env={'MYVAR': 'foo'})
print('setenv:', out)

out = cmd('bash', '-c', 'echo "HOME=\'$HOME\'"').read(full_env={})
print('clear env:', out)

out = cmd('sha1sum').read(stdin="foo")
print('input:', out)

out = sh('cd /tmp; echo $foo', env={'foo': 'local env vars'}).read()
print('real shell commands:', out)
