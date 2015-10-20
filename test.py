#! /usr/bin/env python

from duct import cmd, sh, CheckedError
from pathlib import Path
from nose.tools import eq_, raises


def test_hello_world():
    out = sh('echo "hello  world"').read()
    eq_("hello  world", out)


def test_result():
    result = sh('echo more stuff').run(stdout=str)
    eq_("more stuff\n", result.stdout)


def test_bytes():
    out = sh('head -c 10 /dev/zero').read(stdout=bytes)
    eq_(b'\x00'*10, out)


@raises(CheckedError)
def test_error():
    cmd('false').run()


def test_check():
    # Test both the top level and command level check params.
    eq_(1, cmd('false').run(check=False).status)
    eq_(0, cmd('false', check=False).run().status)


def test_pipe():
    out = sh('head -c 3 /dev/zero').pipe('sed', 's/./a/g').read()
    eq_("aaa", out)


def test_then():
    eq_('hi', cmd('true').then('echo', 'hi').read())
    eq_('', cmd('false').then('echo', 'hi').read(check=False))


def test_nesting():
    innermost = cmd('true').then('cat')
    middle = cmd('true').then(innermost)
    out = sh('echo hi').pipe(middle).read()
    eq_('hi', out)


def test_cwd():
    # Test cwd at both the top level and the command level, and that either can
    # be a pathlib Path.
    eq_('/tmp', cmd('pwd').read(cwd='/tmp'))
    eq_('/tmp', cmd('pwd').read(cwd=Path('/tmp')))
    eq_('/tmp', cmd('pwd', cwd='/tmp').read(cwd='/something/else'))
    eq_('/tmp', cmd('pwd', cwd=Path('/tmp')).read(cwd='/something/else'))


def test_env():
    # Test env at both the top level and the command level, and that values can
    # be pathlib Paths.
    eq_("/", sh("bash -c 'echo $x'").read(env={'x': '/'}))
    eq_("/", sh("bash -c 'echo $x'").read(env={'x': Path('/')}))
    eq_("/", sh("bash -c 'echo $x'", env={'x': '/'}).read())
    eq_("/", sh("bash -c 'echo $x'", env={'x': Path('/')}).read())


def test_full_env():
    eq_("", sh("bash -c 'echo $x'", full_env={}).read(env={'x': 'X'}))


def test_stdin():
    # TODO: This parameter will change to be called "input".
    out = cmd('sha1sum').read(stdin="foo")
    eq_('0beec7b5ea3f0fdbc95d0dd47f3c5bc275da8a33  -', out)
