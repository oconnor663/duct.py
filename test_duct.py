# coding=UTF-8

import binascii
import os
import sys
import tempfile
import textwrap
import threading
import time

from pytest import raises, mark

import duct
from duct import cmd, StatusError

try:
    from pathlib import Path

    has_pathlib = True
except ImportError:
    has_pathlib = False

NEWLINE = os.linesep.encode()

# Windows-compatible commands to mimic Unix
# -----------------------------------------


def exit_cmd(n):
    return cmd("python", "-c", "import sys; sys.exit({0})".format(n))


def true():
    return exit_cmd(0)


def false():
    return exit_cmd(1)


def cat_cmd():
    return cmd(
        "python", "-c", "import sys, shutil; shutil.copyfileobj(sys.stdin, sys.stdout)"
    )


def echo_cmd(s):
    return cmd("python", "-c", 'import sys; print(" ".join(sys.argv[1:]))', s)


def echo_err_cmd(s):
    return cmd(
        "python",
        "-c",
        'import sys; sys.stderr.write(" ".join(sys.argv[1:]) + "\\n")',
        s,
    )


def sleep_cmd(seconds):
    return cmd("python", "-c", "import time; time.sleep({})".format(seconds))


def head_bytes(c):
    code = textwrap.dedent(
        """\
        import os
        # PyPy3 on Travis has a wonky bug where stdin and stdout can't read
        # unicode. This is a workaround. The bug doesn't repro on Arch, though,
        # so presumably it'll be fixed when they upgrade eventually.
        stdin = os.fdopen(0, 'r')
        stdout = os.fdopen(1, 'w')
        input_str = stdin.read({0})
        stdout.write(input_str)
        """.format(
            c
        )
    )
    return cmd("python", "-c", code)


def pwd():
    return cmd("python", "-c", "import os; print(os.getcwd())")


def echo_var(var_name):
    code = textwrap.dedent(
        """\
        import os
        print(os.environ.get("{0}", ""))
        """.format(
            var_name
        )
    )
    return cmd("python", "-c", code)


def echo_x():
    return echo_var("x")


def replace(a, b):
    code = textwrap.dedent(
        """\
        import sys
        input_str = sys.stdin.read()
        sys.stdout.write(input_str.replace({0}, {1}))
        """.format(
            repr(a), repr(b)
        )
    )
    return cmd("python", "-c", code)


# utilities
# ---------


def mktemp():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    return path


# tests
# -----


def test_hello_world():
    out = echo_cmd("hello world").read()
    assert "hello world" == out


def test_result():
    output = echo_cmd("more stuff").stdout_capture().run()
    assert b"more stuff" + NEWLINE == output.stdout
    assert output.stderr is None
    assert 0 == output.status


def test_start():
    handle1 = echo_cmd("one").stdout_capture().start()
    handle2 = echo_cmd("two").stdout_capture().start()
    result1 = handle1.wait()
    result2 = handle2.wait()
    assert b"one" + NEWLINE == result1.stdout
    assert result1.stderr is None
    assert 0 == result1.status
    assert b"two" + NEWLINE == result2.stdout
    assert result2.stderr is None
    assert 0 == result2.status


def test_bytes():
    out = head_bytes(10).stdin_bytes(b"\x00" * 100).read()
    assert "\x00" * 10 == out


def test_nonzero_status_throws():
    with raises(duct.StatusError):
        false().run()


def test_unchecked():
    assert 1 == false().unchecked().run().status
    with raises(StatusError) as e:
        false().run()
    assert e.value.output.status == 1


def test_unchecked_with_pipe():
    zero = exit_cmd(0)
    one = exit_cmd(1)
    two = exit_cmd(2)

    # Right takes precedence over left.
    output = one.pipe(two).unchecked().run()
    assert output.status == 2

    # But not if the right is unchecked.
    output = one.pipe(two.unchecked()).unchecked().run()
    assert output.status == 1

    # But right takes precedence again if both are unchecked.
    output = one.unchecked().pipe(two.unchecked()).run()
    assert output.status == 2

    # Unless the right status is 0.
    output = one.unchecked().pipe(zero).run()
    assert output.status == 1


def test_pipe():
    out = head_bytes(3).pipe(replace("x", "a")).stdin_bytes("xxxxxxxxxx").read()
    assert "aaa" == out


def test_pipe_SIGPIPE():
    """On the left side of the pipe, run a command that outputs text forever.
    That program should receive SIGPIPE when the right side terminates."""
    zeroes_code = textwrap.dedent(
        """\
        import sys
        try:
            while True:
                sys.stdout.write('0')
        except Exception:
            pass
        """
    )
    zeroes = cmd("python", "-c", zeroes_code)
    out = zeroes.unchecked().pipe(head_bytes(5)).read()
    assert "00000" == out


def test_nesting():
    inner = cat_cmd().pipe(replace("i", "o"))
    out = echo_cmd("hi").pipe(inner).read()
    assert "ho" == out


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


def test_dir_with_relative_paths():
    # We need to make sure relative exe paths are valid even when we're using
    # `dir`. Subprocess spawning on Unix doesn't behave that way by default, so
    # duct absolutifies relative paths in that case, and that's what we're
    # testing here.
    child_working_dir = tempfile.mkdtemp()
    interpreter_path = sys.executable
    interpreter_dir = os.path.dirname(interpreter_path)
    interpreter_relative_path = os.path.join(".", os.path.basename(interpreter_path))
    current_dir = os.getcwd()
    try:
        os.chdir(interpreter_dir)
        # Run an empty Python program. This will succeed if the path to the
        # interpreter is valid, but it will fail if the path is interpreted
        # relative to the child's working dir.
        cmd(interpreter_relative_path, "-c", "").dir(child_working_dir).run()
    finally:
        os.chdir(current_dir)


def test_env():
    # Test env with both strings and Pathlib paths.
    assert "foo" == echo_x().env("x", "foo").read()
    if has_pathlib:
        assert "foo" == echo_x().env("x", Path("foo")).read()


def test_env_remove():
    assert "foo" == echo_x().env("x", "foo").env_remove("x").read()
    assert "" == echo_x().env_remove("x").env("x", "foo").read()
    # Make sure the parent environment gets filtered too. Note that this also
    # exercises our case handling on Windows. The "x" gets converted to "X"
    # internally, as it does in the Python interpreter.
    os.environ["x"] = "waa"
    assert "waa" == echo_x().read()
    assert "" == echo_x().env_remove("x").read()
    del os.environ["x"]


def test_full_env():
    # Wrap echo to preserve PATH and, on Windows, SYSTEMROOT. Without the
    # latter, basic Python features like `import os` will fail. We originally
    # didn't need PATH, but at some point prior to between 2022 and 2025 macOS
    # tests started failing without it.
    clear_env = {"foo": "bar", "PATH": os.environ["PATH"]}
    if os.name == "nt":
        clear_env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    assert "bar" == echo_var("foo").full_env(clear_env).read()
    assert "" == echo_x().full_env(clear_env).env("x", "foo").read()


def test_stdin_bytes():
    out = replace("o", "a").stdin_bytes("foo").read()
    assert "faa" == out


def test_stdin():
    temp = mktemp()
    with open(temp, "w") as f:
        f.write("foo")
    # with a file path
    out = replace("o", "a").stdin_path(temp).read()
    assert "faa" == out
    # with a Path path
    if has_pathlib:
        out = replace("o", "b").stdin_path(Path(temp)).read()
        assert "fbb" == out
    # with an open file
    with open(temp) as f:
        out = replace("o", "c").stdin_file(f).read()
        assert "fcc" == out
    # with explicit DEVNULL
    out = replace("o", "d").stdin_null().read()
    assert "" == out


def test_stdout():
    # with a file path
    temp = mktemp()
    echo_cmd("hi").stdout_path(temp).run()
    with open(temp) as f:
        assert "hi\n" == f.read()
    # with a Path path
    if has_pathlib:
        temp = mktemp()
        echo_cmd("hi").stdout_path(Path(temp)).run()
        with open(temp) as f:
            assert "hi\n" == f.read()
    # with an open file
    temp = mktemp()
    with open(temp, "w") as f:
        echo_cmd("hi").stdout_file(f).run()
    with open(temp) as f:
        assert "hi\n" == f.read()
    # to /dev/null
    out = echo_cmd("hi").stdout_null().read()
    assert "" == out
    # to stderr
    output = echo_cmd("hi").stdout_to_stderr().stdout_capture().stderr_capture().run()
    assert b"" == output.stdout
    assert b"hi" + NEWLINE == output.stderr


def test_stderr():
    # with a file path
    temp = mktemp()
    echo_cmd("hi").stdout_to_stderr().stderr_path(temp).run()
    with open(temp) as f:
        assert "hi\n" == f.read()
    # with a Path path
    if has_pathlib:
        temp = mktemp()
        echo_cmd("hi").stdout_to_stderr().stderr_path(Path(temp)).run()
        with open(temp) as f:
            assert "hi\n" == f.read()
    # with an open file
    temp = mktemp()
    with open(temp, "w") as f:
        echo_cmd("hi").stdout_to_stderr().stderr_file(f).run()
    with open(temp) as f:
        assert "hi\n" == f.read()
    # to /dev/null
    out = echo_cmd("hi").stdout_to_stderr().stderr_null().read()
    assert "" == out
    # to stdout
    output = (
        echo_cmd("hi")
        .stdout_to_stderr()
        .stderr_to_stdout()
        .stdout_capture()
        .stderr_capture()
        .run()
    )
    assert b"hi" + NEWLINE == output.stdout
    assert b"" == output.stderr


@mark.skipif(not has_pathlib, reason="pathlib not installed")
def test_commands_can_be_paths():
    tempdir = tempfile.mkdtemp()
    path = Path(tempdir, "script.bat")
    # Note that Path.open() rejects Python 2 non-unicode strings.
    with open(str(path), "w") as f:
        if os.name == "nt":
            f.write("@echo off\n")
        else:
            f.write("#! /bin/sh\n")
        f.write("echo some stuff\n")
    path.chmod(0o755)
    assert "some stuff" == cmd(path).read()


def test_pipe_returns_rightmost_error():
    # Failure on the right.
    with raises(StatusError) as e:
        true().pipe(false()).run()
    assert 1 == e.value.output.status

    # Failure on the left.
    with raises(StatusError) as e:
        false().pipe(true()).run()
    assert 1 == e.value.output.status

    # Both sides are failures. The right error code takes precedence.
    with raises(StatusError) as e:
        false().pipe(exit_cmd(3)).run()
    assert 3 == e.value.output.status


def test_checked_error_contains_status():
    try:
        exit_cmd(123).run()
    except duct.StatusError as e:
        assert "123" in str(e)


def test_DaemonicThread_reraises_exceptions():
    def t():
        raise ZeroDivisionError

    thread = duct.DaemonicThread(t)
    thread.start()
    with raises(ZeroDivisionError):
        thread.join()

    # Kick off another DaemonicThread that will never exit. This tests that we
    # set the daemon flag correctly, otherwise the whole test suite will hang
    # at the end.
    duct.DaemonicThread(lambda: time.sleep(1000000)).start()


def test_invalid_io_args():
    with raises(TypeError):
        cmd("foo").stdin_bytes(1.0).run()
    with raises(TypeError):
        cmd("foo").stdin_path(1.0).run()
    with raises(TypeError):
        cmd("foo").stdout_path(1.0).run()
    with raises(TypeError):
        cmd("foo").stderr_path(1.0).run()


def test_write_error_in_input_thread():
    """The standard Linux pipe buffer is 64 KB, so we pipe 100 KB into a
    program that reads nothing. That will cause the writer thread to block on
    the pipe, and then that write will fail. Test that we catch this
    BrokenPipeError."""
    test_input = "\x00" * 100 * 1000
    true().stdin_bytes(test_input).run()


def test_string_mode_returns_unicode():
    """In Python 2, reading a file in text mode still returns a raw string,
    instead of a unicode string. Make sure we convert."""
    out = echo_cmd("hi").read()
    assert isinstance(out, type(""))


def test_repr_round_trip():
    """Check that our repr() output is exactly the same as the syntax used to
    create the expression. Use single-quoted string values, because that's what
    repr() emits, and don't use bytes literals, because Python 2 won't emit
    them."""

    expressions = [
        "cmd('foo').stdin_bytes('a').stdout_capture().stderr_capture()",
        "cmd('foo').stdin_path('a').stdout_path('b').stderr_path('c')",
        "cmd('foo').stdin_file(0).stdout_file(0).stderr_file(0)",
        "cmd('foo').stdin_null().stdout_null().stderr_null()",
        "cmd('foo').stdout_to_stderr().stderr_to_stdout()",
        "cmd('foo').stdout_stderr_swap().before_spawn(0)",
        "cmd('foo').env('a', 'b').full_env({}).env_remove('c')",
        "cmd('foo').pipe(cmd('bar').dir('stuff').unchecked())",
    ]
    for expression in expressions:
        assert repr(eval(expression)) == expression


def test_swap_and_redirect_at_same_time():
    """We need to make sure that doing e.g. stderr_to_stdout while also doing
    stdout_capture means that stderr joins the redirected stdout, rather than
    joining what stdout used to be."""
    err_out = echo_cmd("hi").stdout_to_stderr().stderr_to_stdout().read()
    assert err_out == "hi"


@mark.skipif(not has_pathlib, reason="pathlib not installed")
def test_run_local_path():
    """Trying to execute 'test.sh' without the leading dot fails in bash and
    subprocess.py. But it needs to succeed with Path('test.sh'), because
    there's no difference between that and Path('./test.sh')."""
    if os.name == "nt":
        extension = ".bat"
        code = textwrap.dedent(
            """\
            @echo off
            echo foo
            """
        )
    else:
        extension = ".sh"
        code = textwrap.dedent(
            """\
            #! /bin/sh
            echo foo
            """
        )
    # Use a random name just in case.
    random_letters = binascii.hexlify(os.urandom(4)).decode()
    local_script = "test_" + random_letters + extension
    script_path = Path(local_script)
    try:
        with script_path.open("w") as f:
            f.write(code)
        script_path.chmod(0o755)
        assert "foo" == cmd(script_path).read()
    finally:
        script_path.unlink()


try:
    # not defined in Python 2 (or pypy3)
    PROGRAM_NOT_FOUND_ERROR = FileNotFoundError
except NameError:
    PROGRAM_NOT_FOUND_ERROR = OSError


@mark.skipif(not has_pathlib, reason="pathlib not installed")
def test_local_path_doesnt_match_PATH():
    echo_path = Path("echo")
    assert not echo_path.exists(), "This path is supposed to be nonexistent."
    with raises(PROGRAM_NOT_FOUND_ERROR):
        cmd(echo_path).run()


def test_unicode():
    # Windows has very wonky Unicode handling in command line params, so
    # instead of worrying about that we just test that we can send UTF-8 input
    # and read UTF-8 output.
    in_str = "日本語"
    cat = head_bytes(-1)
    out = cat.stdin_bytes(in_str).read()
    assert out == "日本語"

    output = cat.stdin_bytes(in_str).stdout_capture().run()
    assert output.stdout == in_str.encode("utf8")


def test_wait():
    input_bytes = b"some really nice input"
    take = 10
    handle = (
        cat_cmd()
        .stdin_bytes(input_bytes)
        .pipe(head_bytes(take))
        .stdout_capture()
        .start()
    )
    output = handle.wait()
    assert output.status == 0
    assert output.stdout == input_bytes[:take]
    assert output.stderr is None


def test_poll():
    handle = (
        echo_err_cmd("error stuff")
        .pipe(echo_cmd("output stuff"))
        .stdout_capture()
        .stderr_capture()
        .start()
    )
    output = None
    while output is None:
        output = handle.poll()
    assert output.status == 0
    assert output.stdout == b"output stuff" + NEWLINE
    assert output.stderr == b"error stuff" + NEWLINE


def test_wait_and_kill():
    handle = sleep_cmd(1000000).pipe(cat_cmd()).env("A", "B").start()
    assert handle.poll() is None
    assert handle.poll() is None
    handle.kill()
    # Twice to exercise the already-waited branches.
    handle.kill()
    with raises(StatusError):
        handle.wait()


def test_right_side_fails_to_start():
    # Python 3 raises the FileNotFoundError, but Python 2 is less consistent.
    with raises(Exception) as e1:
        open("file_that_doesnt_exist")
    not_found_errno = e1.value.errno

    with raises(Exception) as e2:
        sleep_cmd(1000000).pipe(cmd("nonexistent_command_abc123")).run()
    assert e2.value.errno == not_found_errno


def test_before_spawn():
    def callback_inner(command, kwargs):
        command.append("inner")

    def callback_outer(command, kwargs):
        command.append("outer")

    out = (
        echo_cmd("some")
        .before_spawn(callback_inner)
        .before_spawn(callback_outer)
        .read()
    )
    assert out == "some outer inner"


def test_stdout_stderr_swap():
    output = (
        echo_cmd("err")
        .stdout_to_stderr()
        .pipe(echo_cmd("out"))
        .stdout_stderr_swap()
        .stdout_capture()
        .stderr_capture()
        .run()
    )
    assert output.status == 0
    assert output.stdout == b"err" + NEWLINE
    assert output.stderr == b"out" + NEWLINE


def test_reader():
    reader = cat_cmd().stdin_bytes("abc\ndef\n123").reader()
    # Readlines is provided by BufferedIOBase, so this tests that we've
    # inherited from it correctly.
    lines = reader.readlines()
    assert lines == [b"abc" + NEWLINE, b"def" + NEWLINE, b"123"]
    assert reader._read_pipe is None, "has been awaited"


def test_reader_eof():
    reader = cat_cmd().stdin_bytes("abc\ndef\n123").reader()
    assert reader._read_pipe is not None, "not awaited yet"
    reader.read()
    assert reader._read_pipe is None, "has been awaited"


def test_reader_positive_size():
    input_bytes = b"some stuff"
    reader = cat_cmd().stdin_bytes(input_bytes).reader()
    bytes_read = 0
    while bytes_read < len(input_bytes):
        reader.read(1)
        bytes_read += 1
    # The child hasn't been awaited yet, because although we happen to know
    # we're supposed to be at EOF now, we haven't actually read it yet.
    assert reader._read_pipe is not None, "not awaited yet"

    # Now read EOF and check that everything gets cleaned up.
    assert reader.read(1) == b""
    assert reader._read_pipe is None, "has been awaited"


def test_reader_close():
    reader = sleep_cmd(1000000).reader()
    reader.close()
    assert reader._read_pipe is None
    with raises(StatusError):
        reader.read()


def test_reader_with():
    reader = sleep_cmd(1000000).reader()
    with reader:
        pass
    assert reader._read_pipe is None
    with raises(StatusError):
        reader.read()


def test_kill_with_grandchild():
    # We're going to start a child process, and that child is going to start a
    # grandchild. The grandchild is going to sleep forever. We'll read some
    # output from the child to make sure it's done starting the grandchild, and
    # then we'll kill the child. Now, the grandchild will not be killed, and it
    # will still hold a write handle to the stdout pipe. So this tests that the
    # wait done by kill only waits on the child to exit, and does not wait on
    # IO to finish.
    #
    # This test leaks the grandchild process. I'm sorry.

    grandchild_code = r"""
import time

time.sleep(24 * 60 * 60)  # sleep for 1 day
"""

    child_code = r"""
import subprocess
import sys

p = subprocess.Popen(["python", "-c", '''{}'''])
print("started")
sys.stdout.flush()
p.wait()
""".format(
        grandchild_code
    )

    # Capturing stderr means an IO thread is spawned, even though we're using a
    # ReaderHandle to read stdout. What we're testing here is that kill()
    # doesn't wait on that IO thread.
    reader = cmd("python", "-c", child_code).stderr_capture().reader()
    # Read "started" from the child to make sure we don't kill it before it
    # starts the grandchild.
    assert reader.read(7) == b"started"
    # Ok, this had better not block!
    reader.kill()
    # Incidentally this also implicitly tests that background threads are
    # daemonic, like test_DaemonicThread_reraises_exceptions does. Otherwise
    # the test suite will block on exit.


def test_pids():
    handle = echo_cmd("hi").start()
    assert len(handle.pids()) == 1
    assert type(handle.pids()[0]) is int
    handle.wait()

    reader = echo_cmd("hi").reader()
    assert len(reader.pids()) == 1
    assert type(reader.pids()[0]) is int
    reader.read()

    handle = echo_cmd("hi").pipe(cat_cmd().stdout_null().pipe(cat_cmd())).start()
    assert len(handle.pids()) == 3
    assert type(handle.pids()[0]) is int
    assert type(handle.pids()[1]) is int
    handle.wait()

    reader = echo_cmd("hi").pipe(cat_cmd().stdout_null().pipe(cat_cmd())).reader()
    assert len(reader.pids()) == 3
    assert type(reader.pids()[0]) is int
    assert type(reader.pids()[1]) is int
    reader.read()


# This test was added after the release of Python 3.9, which included a
# behavior change that caused a crash in this case. There wasn't previously a
# explicit test for this, but I got lucky and one of the doctests hit it.
# (Example run: https://github.com/oconnor663/duct.py/runs/1488376578)
#
# The behavior change in Python 3.9 is that Popen.send_signal (which is called
# by Popen.kill, which we call in SharedChild.kill) now calls Popen.poll first,
# as a best-effort check to make sure the child's PID hasn't already been freed
# for reuse. If the child has not yet exited, this is effectively no different
# from before. However, if the child has exited, this may reap the child, which
# was not previously possible. This test guarantees that the child has exited
# before kill, and then makes sure kill doesn't crash.
def test_kill_after_child_exit():
    # Create a child process and wait for it to exit, without actually waiting
    # on it and reaping it, by reading its output. We can't use the .read()
    # method for this, because that would actually wait on it and reap it, so
    # we create our own pipe manually.
    pipe_reader, pipe_writer = os.pipe()
    handle = echo_cmd("hi").stdout_file(pipe_writer).start()
    os.close(pipe_writer)
    reader_file = os.fdopen(pipe_reader, "rb")
    assert reader_file.read() == b"hi" + NEWLINE

    # The child has exited. Now just test that kill doesn't crash.
    handle.kill()


# This is ported from test_wait_try_wait_race in shared_child.rs:
# https://github.com/oconnor663/shared_child.rs/blob/0c1910c83c15fc12444261844f663bd3f162df28/src/lib.rs#L531
def test_wait_poll_race():
    # Make sure that .wait() and .poll() can't race against each other. The
    # scenario we're worried about is:
    #   1. wait() takes the lock, set the state to Waiting, and releases the lock.
    #   2. poll() swoops in, takes the lock, sees the Waiting state, and returns Ok(None).
    #   3. wait() resumes, actually calls waitit(), observes the child has exited, retakes the
    #      lock, reaps the child, and sets the state to Exited.
    # A race like this could cause .poll() to report that the child hasn't
    # exited, even if in fact the child exited long ago. A subsequent call to
    # .poll() would almost certainly report Ok(Some(_)), but the first call is
    # still a bug. The way to prevent the bug is by making .wait() do a
    # non-blocking call to waitid() before releasing the lock.
    test_duration_secs = 1
    env_var_name = "RACE_TEST_SECONDS"
    if env_var_name in os.environ:
        print(env_var_name, os.environ[env_var_name])
        test_duration_secs = int(os.environ[env_var_name])
    test_start = time.time()
    iterations = 1
    while True:
        # Start a child that will exit immediately.
        child = duct.SharedChild(["python", "-c", ""])
        # Wait for the child to exit, without updating the SharedChild state.
        if duct.HAS_WAITID:
            os.waitid(os.P_PID, child._child.pid, os.WEXITED | os.WNOWAIT)
        else:
            # For the platforms where we use os.waitid, we have to be careful
            # not to reap the child. But for other platforms, we're going to
            # fall back to Popen.wait anway, so it should be find to do it
            # here.
            child._child.wait()

        # Spawn two threads, one to wait() and one to poll(). It should be
        # impossible for the poll thread to return None at this point. However,
        # we want to make sure there's no race condition between them, where
        # the wait() thread has said it's waiting and released the child lock
        # but hasn't yet actually waited.

        def wait_thread_fn():
            child.wait()

        wait_thread = threading.Thread(target=wait_thread_fn)
        wait_thread.start()

        def poll_thread_fn():
            nonlocal poll_ret
            poll_ret = child.poll()

        poll_ret = None
        poll_thread = threading.Thread(target=poll_thread_fn)
        poll_thread.start()
        wait_thread.join()
        poll_thread.join()
        test_time_so_far = time.time() - test_start
        assert (
            poll_ret is not None
        ), f"encountered the race condition after {test_time_so_far} seconds ({iterations} iterations)"
        iterations += 1

        # If we've met the target test duration (1 sec by default), exit with
        # success. Otherwise keep looping and trying to provoke the race.
        if test_time_so_far >= test_duration_secs:
            return
