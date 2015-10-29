#! /usr/bin/env nosetests

import os
import tempfile
import textwrap

from pytest import raises

import duct
from duct import cmd, sh, DEVNULL, STDOUT, STDERR

try:
    from pathlib import Path
    has_pathlib = True
except ImportError:
    has_pathlib = False


# Windows-compatible commands to mimic Unix
# -----------------------------------------

def exit_cmd(n, **kwargs):
    return cmd('python', '-c', 'import sys; sys.exit({0})'.format(n), **kwargs)


def true(**kwargs):
    return exit_cmd(0, **kwargs)


def false(**kwargs):
    return exit_cmd(1, **kwargs)


def head(c, **kwargs):
    code = textwrap.dedent('''\
        import sys
        input_str = sys.stdin.read({0})
        sys.stdout.write(input_str)
        '''.format(c))
    return cmd('python', '-c', code, **kwargs)


def pwd(**kwargs):
    return cmd('python', '-c', 'import os; print(os.getcwd())', **kwargs)


def echo_x(**kwargs):
    if os.name == 'nt':
        return sh('echo %x%', **kwargs)
    else:
        return sh('echo $x', **kwargs)


def replace(a, b, **kwargs):
    code = textwrap.dedent('''\
        import sys
        input_str = sys.stdin.read()
        sys.stdout.write(input_str.replace({0}, {1}))
        '''.format(repr(a), repr(b)))
    return cmd('python', '-c', code, **kwargs)


# setup and teardown functions for the entire module
# --------------------------------------------------

def setup():
    '''Record the next available file descriptor. When each test finishes,
    check that the next available file descriptor is the same. That means we
    didn't leak any fd's.'''
    global next_fd
    with open(os.devnull) as f:
        next_fd = f.fileno()


def teardown():
    with open(os.devnull) as f:
        new_fd = f.fileno()
    assert next_fd == new_fd, "We leaked a file descriptor!"


# utilities
# ---------

def mktemp():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    return path


# tests
# -----

def test_hello_world():
    out = sh('echo "hello  world"').read()
    assert "hello  world" == out


def test_result():
    result = sh('echo more stuff').run(stdout=str)
    assert "more stuff\n" == result.stdout


def test_bytes():
    out = head(10).read(input="\x00"*100, stdout=bytes)
    assert b'\x00'*10 == out


def test_nonzero_status_throws():
    with raises(duct.CheckedError):
        false().run()


def test_check():
    # Test both the top level and command level check params.
    assert 1 == false().run(check=False).status
    assert 0 == false(check=False).run().status


def test_pipe():
    out = head(3).pipe(replace('x', 'a')).read(input="xxx")
    assert "aaa" == out


def test_then():
    assert 'hi' == true().then(sh('echo hi')).read()
    assert '' == false().then(sh('echo hi')).read(check=False)


def test_nesting():
    innermost = true().then(replace('i', 'o'))
    middle = true().then(innermost)
    out = sh('echo hi').pipe(middle).read()
    assert 'ho' == out


def test_cwd():
    # Test cwd at both the top level and the command level, and that either can
    # be a pathlib Path.
    tmpdir = tempfile.mkdtemp()
    another = tempfile.mkdtemp()
    assert tmpdir == pwd().read(cwd=tmpdir)
    assert tmpdir == pwd(cwd=tmpdir).read(cwd=another)
    if has_pathlib:
        assert tmpdir == pwd().read(cwd=Path(tmpdir))
        assert (tmpdir == pwd(cwd=Path(tmpdir))
                .read(cwd='/something/else'))


def test_env():
    # Test env at both the top level and the command level, and that values can
    # be pathlib Paths.
    assert "foo" == echo_x().read(env={'x': 'foo'})
    assert "foo" == echo_x(env={'x': 'foo'}).read()
    if has_pathlib:
        assert "foo" == echo_x().read(env={'x': Path('foo')})
        assert "foo" == echo_x(env={'x': Path('foo')}).read()


def test_full_env():
    out = "%x%" if os.name is 'nt' else ""
    assert out == echo_x(full_env={}).read(env={'x': 'X'})


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
    out = replace('o', 'a').read(input="foo")
    assert 'faa' == out


def test_stdin():
    temp = mktemp()
    with open(temp, 'w') as f:
        f.write('foo')
    # with a file path
    out = replace('o', 'a').read(stdin=temp)
    assert 'faa' == out
    # with a Path path
    if has_pathlib:
        out = replace('o', 'b').read(stdin=Path(temp))
        assert 'fbb' == out
    # with an open file
    with open(temp) as f:
        out = replace('o', 'c').read(stdin=f)
        assert 'fcc' == out
    # with explicit DEVNULL
    out = replace('o', 'd').read(stdin=DEVNULL)
    assert '' == out


def test_stdout():
    # with a file path
    temp = mktemp()
    sh('echo hi').run(stdout=temp)
    assert 'hi\n' == open(temp).read()
    # with a Path path
    if has_pathlib:
        temp = mktemp()
        sh('echo hi').run(stdout=Path(temp))
        assert 'hi\n' == open(temp).read()
    # with an open file
    temp = mktemp()
    sh('echo hi').run(stdout=temp)
    assert 'hi\n' == open(temp).read()
    # with explicit DEVNULL
    out = sh('echo hi', stdout=DEVNULL).read()
    assert '' == out
    # to STDERR
    result = sh('echo hi', stdout=STDERR).run(stdout=str, stderr=str)
    assert '' == result.stdout
    assert 'hi\n' == result.stderr
    # from stderr with STDOUT (note Windows would output any space before >)
    result = sh('echo hi>&2', stderr=STDOUT).run(stdout=str, stderr=bytes)
    assert 'hi\n' == result.stdout
    assert b'' == result.stderr
    # full swap
    result = (sh('echo hi&& echo lo>&2', stdout=STDERR, stderr=STDOUT)
              .run(stdout=str, stderr=str))
    assert 'lo\n' == result.stdout
    assert 'hi\n' == result.stderr


def test_commands_can_be_paths():
    if has_pathlib:
        tempdir = tempfile.mkdtemp()
        path = Path(tempdir, "script.bat")
        with path.open('w') as f:
            if os.name == 'nt':
                f.write('@echo off\n')
            else:
                f.write('#! /bin/sh\n')
            f.write('echo some stuff\n')
        path.chmod(0o755)
        assert 'some stuff' == cmd(path).read()
        assert 'some stuff\n' == sh(path).read(trim=False)


def test_subshell():
    # Note, don't put a space before the redirect, because Windows will keep
    # that in the output.
    c = sh("echo foo>&2").then(false())
    out = c.subshell(check=False, stderr=STDOUT).read()
    assert "foo" == out


def test_kwargs_prohibited_with_expression_value():
    # This should throw even before the command is run.
    with raises(TypeError):
        cmd("foo").pipe(cmd("bar"), check=False)


def test_pipe_returns_rightmost_error():
    assert 1 == true().pipe(false()).run(check=False).status
    assert 1 == false().pipe(false()).run(check=False).status
    assert (3 == false().pipe(exit_cmd(3)).run(check=False)
            .status)


def test_checked_error_contains_status():
    try:
        exit_cmd(123).run()
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


def test_write_error_in_input_thread():
    '''The standard Linux pipe buffer is 64 KB, so we pipe 100 KB into a
    program that reads nothing. That will cause the writer thread to block on
    the pipe, and then that write will fail. Test that we catch this
    BrokenPipeError.'''
    test_input = '\x00' * 100 * 1000
    true().run(input=test_input)
