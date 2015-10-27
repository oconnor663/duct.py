#! /usr/bin/env nosetests

import os
import tempfile

from pytest import raises

import duct
from duct import cmd, sh, DEVNULL, STDOUT, STDERR

try:
    from pathlib import Path
    has_pathlib = True
except ImportError:
    has_pathlib = False


def test_hello_world():
    out = sh('echo "hello  world"').read()
    assert "hello  world" == out


def test_result():
    result = sh('echo more stuff').run(stdout=str)
    assert "more stuff\n" == result.stdout


def test_bytes():
    out = sh('head -c 10 /dev/zero').read(stdout=bytes)
    assert b'\x00'*10 == out


def test_nonzero_status_throws():
    with raises(duct.CheckedError):
        cmd('false').run()


def test_check():
    # Test both the top level and command level check params.
    assert 1 == cmd('false').run(check=False).status
    assert 0 == cmd('false', check=False).run().status


def test_pipe():
    out = sh('head -c 3 /dev/zero').pipe('sed', 's/./a/g').read()
    assert "aaa" == out


def test_then():
    assert 'hi' == cmd('true').then('echo', 'hi').read()
    assert '' == cmd('false').then('echo', 'hi').read(check=False)


def test_nesting():
    innermost = cmd('true').then('cat')
    middle = cmd('true').then(innermost)
    out = sh('echo hi').pipe(middle).read()
    assert 'hi' == out


def test_cwd():
    # Test cwd at both the top level and the command level, and that either can
    # be a pathlib Path.
    assert '/tmp' == cmd('pwd').read(cwd='/tmp')
    assert '/tmp' == cmd('pwd', cwd='/tmp').read(cwd='/something/else')
    if has_pathlib:
        assert '/tmp' == cmd('pwd').read(cwd=Path('/tmp'))
        assert ('/tmp' == cmd('pwd', cwd=Path('/tmp'))
                .read(cwd='/something/else'))


def test_env():
    # Test env at both the top level and the command level, and that values can
    # be pathlib Paths.
    assert "/" == sh("bash -c 'echo $x'").read(env={'x': '/'})
    assert "/" == sh("bash -c 'echo $x'", env={'x': '/'}).read()
    if has_pathlib:
        assert "/" == sh("bash -c 'echo $x'").read(env={'x': Path('/')})
        assert "/" == sh("bash -c 'echo $x'", env={'x': Path('/')}).read()


def test_full_env():
    assert "" == sh("bash -c 'echo $x'", full_env={}).read(env={'x': 'X'})


def test_env_with_full_env_throws():
    # This should throw even before the command is run.
    with raises(ValueError):
        cmd("foo", env={}, full_env={})


def test_input_with_stdin_throws():
    # This should throw even before the command is run.
    with raises(ValueError):
        cmd("foo", input="foo", stdin="foo")


def test_undefined_keyword_throws():
    # This should throw even before the command is run.
    with raises(TypeError):
        cmd("foo", junk_keyword=True)


def test_input():
    out = cmd('sha1sum').read(input="foo")
    assert '0beec7b5ea3f0fdbc95d0dd47f3c5bc275da8a33  -' == out


def test_stdin():
    tempfd, temp = tempfile.mkstemp()
    with os.fdopen(tempfd, 'w') as f:
        f.write('foo')
    expected = '0beec7b5ea3f0fdbc95d0dd47f3c5bc275da8a33  -'
    # with a file path
    out = cmd('sha1sum').read(stdin=temp)
    assert expected == out
    # with a Path path
    if has_pathlib:
        out = cmd('sha1sum').read(stdin=Path(temp))
        assert expected == out
    # with an open file
    with open(temp) as f:
        out = cmd('sha1sum').read(stdin=f)
        assert expected == out
    # with explicit DEVNULL
    out = cmd('sha1sum').read(stdin=DEVNULL)
    assert 'da39a3ee5e6b4b0d3255bfef95601890afd80709  -' == out


def test_stdout():
    # with a file path
    with tempfile.NamedTemporaryFile() as temp:
        sh('echo hi').run(stdout=temp.name)
        assert 'hi\n' == open(temp.name).read()
    # with a Path path
    if has_pathlib:
        with tempfile.NamedTemporaryFile() as temp:
            sh('echo hi').run(stdout=Path(temp.name))
            assert 'hi\n' == open(temp.name).read()
    # with an open file
    with tempfile.NamedTemporaryFile() as temp:
        sh('echo hi').run(stdout=temp)
        assert 'hi\n' == open(temp.name).read()
    # with explicit DEVNULL
    out = sh('echo hi', stdout=DEVNULL).read()
    assert '' == out
    # to STDERR
    result = sh('echo hi', stdout=STDERR).run(stdout=str, stderr=str)
    assert '' == result.stdout
    assert 'hi\n' == result.stderr
    # from stderr with STDOUT
    result = sh('echo hi 1>&2', stderr=STDOUT).run(stdout=bytes, stderr=bytes)
    assert b'hi\n' == result.stdout
    assert b'' == result.stderr
    # full swap
    result = (sh('echo hi; echo lo 1>&2', stdout=STDERR, stderr=STDOUT)
              .run(stdout=str, stderr=str))
    assert 'lo\n' == result.stdout
    assert 'hi\n' == result.stderr


def test_commands_can_be_paths():
    if has_pathlib:
        echo = Path('/bin/echo')
        assert 'foo' == cmd(echo, 'foo').read()
        assert '\n' == sh(echo).read(trim=False)


def test_subshell():
    c = sh("echo foo >&2 ; false")
    out = c.subshell(check=False, stderr=STDOUT).read()
    assert "foo" == out


def test_kwargs_prohibited_with_expression_value():
    # This should throw even before the command is run.
    with raises(TypeError):
        cmd("foo").pipe(cmd("bar"), check=False)


def test_pipe_returns_rightmost_error():
    assert 1 == sh('true').pipe('false').run(check=False).status
    assert 1 == cmd('false').pipe('false').run(check=False).status
    assert (3 == cmd('false').pipe(sh('bash -c "exit 3"')).run(check=False)
            .status)


def test_checked_error_contains_status():
    try:
        sh('bash -c "exit 123"').run()
    except duct.CheckedError as e:
        assert '123' in str(e)


def test_ThreadWithReturn_reraises_exceptions():
    def t():
        raise ZeroDivisionError
    thread = duct.ThreadWithReturn(t)
    thread.start()
    with raises(ZeroDivisionError):
        thread.join()


def test_getting_reader_output_before_join_throws():
    default_context = duct.IOContext()
    _, ioargs = duct.parse_cmd_kwargs(stdout=str, stderr=str)
    with default_context.child_context(ioargs) as iocontext:
        with raises(RuntimeError):
            iocontext.stdout_result()
        with raises(RuntimeError):
            iocontext.stderr_result()
    # Exiting the with-block joins the reader threads, so the output accessors
    # should no longer throw.
    assert '' == iocontext.stdout_result()
    assert '' == iocontext.stderr_result()


def test_invalid_io_args():
    with raises(TypeError):
        cmd('foo', input=1.0).run()
    with raises(TypeError):
        cmd('foo', stdin=1.0).run()
    with raises(TypeError):
        cmd('foo', stdout=1.0).run()
    with raises(TypeError):
        cmd('foo', stderr=1.0).run()
