#! /usr/bin/env nosetests
# coding=UTF-8

import binascii
import os
import tempfile
import textwrap

from pytest import raises, mark

import duct
from duct import cmd, sh, StatusError

try:
    from pathlib import Path
    has_pathlib = True
except ImportError:
    has_pathlib = False

NEWLINE = os.linesep.encode()


# Windows-compatible commands to mimic Unix
# -----------------------------------------

def exit_cmd(n):
    return cmd('python', '-c', 'import sys; sys.exit({0})'.format(n))


def true():
    return exit_cmd(0)


def false():
    return exit_cmd(1)


def head_bytes(c):
    code = textwrap.dedent('''\
        import sys
        input_str = sys.stdin.read({0})
        sys.stdout.write(input_str)
        '''.format(c))
    return cmd('python', '-c', code)


def pwd():
    return cmd('python', '-c', 'import os; print(os.getcwd())')


def echo_x():
    code = textwrap.dedent('''\
        import os
        print(os.environ.get("x", ""))
        ''')
    return cmd('python', '-c', code)


def replace(a, b):
    code = textwrap.dedent('''\
        import sys
        input_str = sys.stdin.read()
        sys.stdout.write(input_str.replace({0}, {1}))
        '''.format(repr(a), repr(b)))
    return cmd('python', '-c', code)


# utilities
# ---------

def mktemp():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    return path


# tests
# -----

def test_hello_world():
    out = sh('echo hello world').read()
    assert "hello world" == out


def test_result():
    result = sh('echo more stuff').capture_stdout().run()
    assert b"more stuff" + NEWLINE == result.stdout
    assert b"" == result.stderr
    assert 0 == result.status


def test_bytes():
    out = head_bytes(10).input(b'\x00'*100).read()
    assert '\x00'*10 == out


def test_nonzero_status_throws():
    with raises(duct.StatusError):
        false().run()


def test_unchecked():
    assert 0 == false().unchecked().run().status
    with raises(StatusError) as e:
        false().run()
    assert e.value.result.status


def test_pipe():
    out = head_bytes(3).pipe(replace('x', 'a')).input("xxxxxxxxxx").read()
    assert "aaa" == out


def test_pipe_SIGPIPE():
    '''On the left side of the pipe, run a command that outputs text forever.
    That program should receive SIGPIPE when the right side terminates.'''
    zeroes_code = textwrap.dedent('''\
        import sys
        try:
            while True:
                sys.stdout.write('0')
        except Exception:
            pass
        ''')
    zeroes = cmd('python', '-c', zeroes_code)
    out = zeroes.pipe(head_bytes(5)).read()
    assert "00000" == out


def test_then():
    print_a = cmd('python', '-c', 'print("A")')
    assert 'A' == true().then(print_a).read()
    assert '' == false().then(print_a).unchecked().read()


def test_nesting():
    innermost = true().then(replace('i', 'o'))
    middle = true().then(innermost)
    out = sh('echo hi').pipe(middle).read()
    assert 'ho' == out


def test_dir():
    # Test dir at both the top level and the command level, and that either can
    # be a pathlib Path. Use realpath() on the paths we get from mkdtemp(),
    # because on OSX there's a symlink in there.
    tmpdir = os.path.realpath(tempfile.mkdtemp())
    another = os.path.realpath(tempfile.mkdtemp())
    assert tmpdir == pwd().dir(tmpdir).read()
    assert tmpdir == pwd().dir(tmpdir).dir(another).read()
    if has_pathlib:
        assert tmpdir == pwd().dir(Path(tmpdir)).read()


def test_env():
    # Test env with both strings and Pathlib paths.
    assert "foo" == echo_x().env('x', 'foo').read()
    if has_pathlib:
        assert "foo" == echo_x().env('x', Path('foo')).read()


def test_full_env():
    # Wrap echo to preserve the SYSTEMROOT variable on Windows. Without this,
    # basic Python features like `import os` will fail.
    clear_env = {}
    if os.name == "nt":
        clear_env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    assert "" == echo_x().full_env(clear_env).env('x', 'foo').read()


def test_input():
    out = replace('o', 'a').input("foo").read()
    assert 'faa' == out


def test_stdin():
    temp = mktemp()
    with open(temp, 'w') as f:
        f.write('foo')
    # with a file path
    out = replace('o', 'a').stdin(temp).read()
    assert 'faa' == out
    # with a Path path
    if has_pathlib:
        out = replace('o', 'b').stdin(Path(temp)).read()
        assert 'fbb' == out
    # with an open file
    with open(temp) as f:
        out = replace('o', 'c').stdin(f).read()
        assert 'fcc' == out
    # with explicit DEVNULL
    out = replace('o', 'd').null_stdin().read()
    assert '' == out


def test_stdout():
    # with a file path
    temp = mktemp()
    sh('echo hi').stdout(temp).run()
    with open(temp) as f:
        assert 'hi\n' == f.read()
    # with a Path path
    if has_pathlib:
        temp = mktemp()
        sh('echo hi').stdout(Path(temp)).run()
        with open(temp) as f:
            assert 'hi\n' == f.read()
    # with an open file
    temp = mktemp()
    with open(temp, 'w') as f:
        sh('echo hi').stdout(f).run()
    with open(temp) as f:
        assert 'hi\n' == f.read()
    # with explicit DEVNULL
    out = sh('echo hi').null_stdout().read()
    assert '' == out
    # to STDERR
    result = (sh('echo hi')
              .stdout_to_stderr()
              .capture_stdout()
              .capture_stderr()
              .run())
    assert b'' == result.stdout
    assert b'hi' + NEWLINE == result.stderr
    # from stderr with STDOUT (note Windows would output any space before >)
    result = (sh('echo hi>&2')
              .stderr_to_stdout()
              .capture_stdout()
              .capture_stderr()
              .run())
    assert b'hi' + NEWLINE == result.stdout
    assert b'' == result.stderr
    # Swapping both ends up with them joined (for better or worse).
    result = (sh('echo hi&& echo lo>&2').stdout_to_stderr().stderr_to_stdout()
              .capture_stdout().capture_stderr().run())
    assert b'hi' + NEWLINE + b'lo' + NEWLINE == result.stdout
    assert b'' == result.stderr


@mark.skipif(not has_pathlib, reason='pathlib not installed')
def test_commands_can_be_paths():
    tempdir = tempfile.mkdtemp()
    path = Path(tempdir, "script.bat")
    # Note that Path.open() rejects Python 2 non-unicode strings.
    with open(str(path), 'w') as f:
        if os.name == 'nt':
            f.write('@echo off\n')
        else:
            f.write('#! /bin/sh\n')
        f.write('echo some stuff\n')
    path.chmod(0o755)
    assert 'some stuff' == cmd(path).read()
    assert 'some stuff' == sh(path).read()


def test_pipe_returns_rightmost_error():
    # Failure on the right.
    with raises(StatusError) as e:
        true().pipe(false()).run()
    assert 1 == e.value.result.status

    # Failure on the left.
    with raises(StatusError) as e:
        false().pipe(true()).run()
    assert 1 == e.value.result.status

    # Both sides are failures. The right error code takes precedence.
    with raises(StatusError) as e:
        false().pipe(exit_cmd(3)).run()
    assert 3 == e.value.result.status


def test_checked_error_contains_status():
    try:
        exit_cmd(123).run()
    except duct.StatusError as e:
        assert '123' in str(e)


def test_ThreadWithReturn_reraises_exceptions():
    def t():
        raise ZeroDivisionError
    thread = duct.ThreadWithReturn(t)
    thread.start()
    with raises(ZeroDivisionError):
        thread.join()


def test_invalid_io_args():
    with raises(TypeError):
        cmd('foo').input(1.0).run()
    with raises(TypeError):
        cmd('foo').stdin(1.0).run()
    with raises(TypeError):
        cmd('foo').stdout(1.0).run()
    with raises(TypeError):
        cmd('foo').stderr(1.0).run()


def test_write_error_in_input_thread():
    '''The standard Linux pipe buffer is 64 KB, so we pipe 100 KB into a
    program that reads nothing. That will cause the writer thread to block on
    the pipe, and then that write will fail. Test that we catch this
    BrokenPipeError.'''
    test_input = '\x00' * 100 * 1000
    true().input(test_input).run()


def test_string_mode_returns_unicode():
    '''In Python 2, reading a file in text mode still returns a raw string,
    instead of a unicode string. Make sure we convert.'''
    out = sh('echo hi').read()
    assert isinstance(out, type(u''))


def test_repr_round_trip():
    '''Check that our repr() output is exactly the same as the syntax used to
    create the expression. Note that expression_repr() sorts keywords
    alphabetically, so we need to do the same here. Also, use single-quoted
    string values, because that's what repr() emits, and don't use bytes
    literals, because Python 2 won't emit them.'''

    expressions = [
        "cmd('foo').unchecked().env('a', 'b').full_env({})",
        "sh('bar').null_stdin().input('')",
        "cmd('foo').pipe(cmd('bar'))",
        "cmd('foo').pipe(sh('bar'))",
        "cmd('foo').then(cmd('bar'))",
        "cmd('foo').then(sh('bar'))",
        "cmd('foo').null_stdout().stdout_to_stderr()",
        "cmd('foo').null_stderr().stderr_to_stdout()",
        "cmd('foo').dir('stuff')",
    ]
    for expression in expressions:
        assert repr(eval(expression)) == expression


def test_swap_and_redirect_at_same_time():
    '''We need to make sure that setting e.g. stderr=STDOUT while also setting
    stdout=CAPTURE means that stderr joins the redirected stdout, rather than
    joining what stdout used to be.'''
    err_out = sh('echo hi>&2').stderr_to_stdout().read()
    assert err_out == 'hi'


@mark.skipif(not has_pathlib, reason='pathlib not installed')
def test_run_local_path():
    '''Trying to execute 'test.sh' without the leading dot fails in bash and
    subprocess.py. But it needs to succeed with Path('test.sh'), because
    there's no difference between that and Path('./test.sh').'''
    if os.name == 'nt':
        extension = '.bat'
        code = textwrap.dedent(u'''\
            @echo off
            echo foo
            ''')
    else:
        extension = '.sh'
        code = textwrap.dedent(u'''\
            #! /bin/sh
            echo foo
            ''')
    # Use a random name just in case.
    random_letters = binascii.hexlify(os.urandom(4)).decode()
    local_script = 'test_' + random_letters + extension
    script_path = Path(local_script)
    try:
        with script_path.open('w') as f:
            f.write(code)
        script_path.chmod(0o755)
        assert 'foo' == cmd(script_path).read()
        assert 'foo' == sh(script_path).read()
    finally:
        script_path.unlink()


try:
    # not defined in Python 2 (or pypy3)
    PROGRAM_NOT_FOUND_ERROR = FileNotFoundError
except NameError:
    PROGRAM_NOT_FOUND_ERROR = OSError


@mark.skipif(not has_pathlib, reason='pathlib not installed')
def test_local_path_doesnt_match_PATH():
    echo_path = Path('echo')
    assert not echo_path.exists(), 'This path is supposed to be nonexistent.'
    with raises(PROGRAM_NOT_FOUND_ERROR):
        cmd(echo_path).run()
    with raises(duct.StatusError):
        sh(echo_path).run()

def test_read_unicode():
    out = sh("echo 日本語").read()
    assert out == u"日本語"
