# Gotchas, bugs, and platform inconsistencies

> In loving memory of Prof. Stan Eisenstat and his legendary course, CS 323.

This document describes the colorful variety of issues that come up when you
use child processes, and the solutions that Duct chooses for them. It's
intended for users who want to understand Duct's behavior better, and also for
library authors who want to compare notes on their own behavior in these cases.

Duct is currently implemented in both
[Python](https://github.com/oconnor663/duct.py) and
[Rust](https://github.com/oconnor663/duct.rs), and it aims to be easily
portable to other languages. Duct's behavior is generally identical across
languages, but this document comments on cases where language differences
affect the implementation.

## Contents

* [Reporting errors by default](#reporting-errors-by-default)
* [Catching pipe errors when writing to standard input](#catching-pipe-errors-when-writing-to-standard-input)
* [Waiting on killed children by default](#waiting-on-killed-children-by-default)
* [Making `kill` thread-safe](#making-kill-thread-safe)
* [Adding `./` to program names given as relative paths](#adding--to-program-names-given-as-relative-paths)
* [Preventing `dir` from affecting relative program paths on Unix](#preventing-dir-from-affecting-relative-program-paths-on-unix)
* [Preventing pipe inheritance races on Windows](#preventing-pipe-inheritance-races-on-windows)
* [Matching platform case-sensitivity for environment variables](#matching-platform-case-sensitivity-for-environment-variables)
* [Cleaning up partially started pipelines](#cleaning-up-partially-started-pipelines)
* [Using IO threads to avoid blocking children](#using-io-threads-to-avoid-blocking-children)
* [Killing grandchild processes?](#killing-grandchild-processes)

## Reporting errors by default

Most programming languages make error checking the default, either by crashing
your program with an exception, or by emitting warnings or compiler errors for
unchecked results. But the child process APIs in most standard libraries
(including Python and Rust) do the opposite, ignoring non-zero exit statuses by
default. That's unfortunate, because most command line utilities helpfully
distinguish between success and failure in their exit status. For example, if
you give the wrong path to a `tar` command:

```bash
> tar xf misspelled_filename.txt
tar: misspelled_filename.txt: Cannot open: No such file or directory
tar: Error is not recoverable: exiting now
> echo $?
2
```

Duct treats a non-zero exit status as an error and propagates it to the caller
by default. For suppressing these errors, Duct provides the `unchecked` method.

## Catching pipe errors when writing to standard input

When writing to a child's stdin, Duct catches and ignores broken pipe errors
(`EPIPE`). That means it's not an error for the child to exit early without
reading all of its input. Most standard libraries get this right.

Notably on Unix, this requires the process to suppress `SIGPIPE`.
Implementations in languages that don't suppress `SIGPIPE` by default (C/C++?)
have no choice but to set a signal handler from library code, which might
conflict with application code or other libraries. There is no good solution to
this problem.

## Waiting on killed children by default

Many languages (Python, Rust, Go) provide a `kill` API that sends `SIGKILL` to
a child process on Unix or calls `TerminateProcess` on Windows. The caller has
to remember to wait on the child afterwards, or it turns into a zombie and
leaks resources. Duct waits by default instead.

`SIGKILL` cannot be caught or ignored, and so waiting will almost always return
quickly. One of the rare exceptions to this is if the child is stuck in an
uninterruptible system call, for example a `read` of an unresponsive FUSE
filesystem. In general, Duct's correctness priorities are:

1. Do not leave zombie children or leak other resources.
2. Do not block in a non-blocking API (`start`, `reader`, `try_wait`, or
   `kill`).
3. Do not let errors pass silently.

In this case #1 takes priority over #2.

However, one important subtlety here is that we can only kill child processes
that we spawned. We can't kill grandchild processes that our children spawned.
(See the entire [Killing grandchild processes?](#killing-grandchild-processes)
section for more on this.) Those grandchild processes might inherit any output
pipes that we gave to the child. And that means that even though we do expect
the child to exit promptly in non-pathological cases, we can't expect a read of
the child's stdout pipe to return EOF promptly. Unkilled grandchildren might
keep it open. So the automatic wait performed by `kill` must _not_ wait on IO
threads to finish. It must only reap zombie children.

A lucky break for us here is that, as long as we reap our own zombie children,
most other cleanup is automatic. Most standard libraries take care of "zombie
threads" for us. (Rust detaches threads in the
[`std::thread::JoinHandle`](https://doc.rust-lang.org/std/thread/struct.JoinHandle.html)
destructor, and Python detaches threads as soon as they're spawned.) So we can
leave IO threads running until any pipe-holding grandchildren exit naturally.
And the OS automatically reaps zombie grandchildren if their parent has exited.
The only active step we need to take is to set the "daemon" flag on IO threads
in Python, so that they don't block the parent process from exiting. (Rust
threads never block exit.)

## Making `kill` thread-safe

On Unix-like platforms there's a race condition between `kill` and `waitpid`.
If a process exits right before you signal it, a waiting thread might clean it
up and free its PID, and then an unrelated process might immediately reuse that
PID. It's not likely, but all of that could happen just before the call to
`kill`, and you might end up killing the unrelated process. This race condition
is why the Rust standard library [doesn't allow shared access to child
processes](https://doc.rust-lang.org/std/process/struct.Child.html#method.kill).

It's possible to avoid this race using a newer POSIX API called
[`waitid`](https://pubs.opengroup.org/onlinepubs/9699919799/functions/waitid.html).
That function has a `WNOWAIT` flag that leaves the child in its zombie state,
so that its PID isn't freed for reuse. That gives the waiting thread a chance
to set a flag to block further kills, before reaping the child. Duct uses this
approach on Unix-like platforms. Windows doesn't have this problem.

A recent update here: As part of a best-effort check for this bug, Python 3.9
[changed the behavior](https://bugs.python.org/issue38630) of `Popen.kill` to
reap child processes that have already exited. This [interacts
poorly](https://github.com/oconnor663/duct.py/commit/5dfae70cc9481051c5e53da0c48d9efa8ff71507)
with code that calls `os.waitid` or `os.waitpid` directly.

## Adding `./` to program names given as relative paths

When you run the command `foo`, it can be ambiguous whether you mean `./foo` in
current directory or e.g. `/usr/bin/foo` in the `PATH`. Different platforms do
different things here: Unix-like platforms usually require the leading `./` for
programs in the current directory, but Windows will accept a bare filename.
Duct defers to the platform for interpreting program names that are given as
strings, but it explicitly prepends `./` to program names that are given as
explicit path types (`pathlib` in Python, `std::path` in Rust) when the path is
relative.

This solves two problems:

- It prevents "command not found" errors on Unix-like platforms for paths to
  programs in the current directory. This is especially important in Python,
  where `pathlib.Path` automatically strips leading dots.
- It prevents paths to a nonexistent local file, which _should_ result in
  "command not found", from instead matching a program in the `%PATH%` on
  Windows.

A recent update here: Rust 1.58 [changed the
behavior](https://blog.rust-lang.org/2022/01/13/Rust-1.58.0.html#reduced-windows-command-search-path)
of `std::process::Command` to exclude the current directory from the search
path on Windows.

## Preventing `dir` from affecting relative program paths on Unix

Windows and Unix take different approaches to setting a child's working
directory. The `CreateProcess` function on Windows has an explicit
`lpCurrentDirectory` argument, while most Unix platforms call `chdir` in
between `fork` and `exec`. Unfortunately, those two approaches give different
results when you have a _relative path_ to the child executable. On Windows the
path is interpreted from the parent's working directory, but on Unix it's
interpreted from the child's.

The Windows behavior is preferable, because it lets you add a `dir` argument
without breaking any existing relative program paths. Duct provides this
behavior on all platforms, by canonicalizing relative program paths on
Unix-like platforms when the `dir` method is in use.

## Preventing pipe inheritance races on Windows

Spawning child processes on Windows involves duplicating pipes and making them
inheritable. Unfortunately, that means that *any* child spawned on other
threads while those pipes are alive [will inherit
them](https://web.archive.org/web/20130610174104/https://support.microsoft.com/kb/315939).
One child might accidentally receive a copy of another child's stdin pipe,
preventing the other child from reading EOF and leading to deadlocks. The Rust
standard library [has an internal
mutex](https://github.com/rust-lang/rust/blob/1.14.0/src/libstd/sys/windows/process.rs#L169-L179)
to prevent this race, but the Python standard library [does
not](https://bugs.python.org/issue24909). In Python, Duct uses its own internal
mutex to prevent this race. That doesn't prevent races with other libraries,
but at least multiple Duct callers on different threads are protected.

## Matching platform case-sensitivity for environment variables

Environment variable names are case-sensitive on Unix but case-insensitive on
Windows, and Duct tries to respect each platform's behavior. Methods like
`env_remove` require keeping an internal map of variables, and map keys are
always case-sensitive, so Duct explicitly converts all variable names to
uppercase on Windows.

Duct makes no guarantees about non-ASCII environment variable names. Their
behavior is implementation-dependent, platform-dependent, programming
language-dependent, and probably also human language-dependent.

## Cleaning up partially started pipelines

If the left half of a pipeline starts successfully, but the right half fails to
start, Duct **kills and awaits** the left half, and then reports the original
error from the right half.

To be clear, "failed to start" doesn't mean "exited with a non-zero status".
Rather, this is the situation where the right side never spawned at all. There
is no exit status, because there was no child process. Most commonly that's
because a command name was misspelled, a path was constructed incorrectly, or
the target program isn't installed. Less commonly, the system may be under
heavy load and failing to spawn new processes in general.

Killing the left side is an unfortunate compromise. It's bad behavior to kill
child processes without being asked to by the caller. An unexpected kill signal
might cause some programs to misbehave or corrupt data. But recall Duct's
correctness priorities:

1. Do not leave zombie children or leak other resources.
2. Do not block in a non-blocking API (`start`, `reader`, `try_wait`, or
   `kill`).
3. Do not let errors pass silently.

Leaving the left side running would violate #1. If the child failed to start
because the system was under heavy load, leaking resources might exacerbate the
problem and make the whole system unrecoverable. Waiting on the left side to
exit on its own would violate #2. Deferring error reporting until the caller
waits would violate #3.

Killing the left side isn't good, but it's the least bad option in a bad
situation. A correct program will only encounter this behavior when the whole
system is suffering from resource exhaustion. The Linux OOM killer might
already be killing child processes randomly in that case, and the parent
already needs to think about failure handling and data corruption.

## Using IO threads to avoid blocking children

When input bytes are supplied or output bytes are captured, Duct's `start`
method uses background threads to do IO, so that IO makes progress even if
`wait` is never called. Duct's `reader` method doesn't use a thread for
standard output, since that's left to the caller, but it still uses background
threads to supply input bytes or to capture standard error.

Consider the following scenario. You want to spawn two child processes, which
will exchange messages with each other in the background somehow, e.g. using
D-Bus. You also want to capture the output of each process. Your code might
look like this:

```python
handle1 = cmd("child1").stdout_capture().start()
handle2 = cmd("child2").stdout_capture().start()
output1 = handle1.wait().stdout
output2 = handle2.wait().stdout
```

If Duct handled captured output without threads, e.g. using a read loop inside
of `wait`, that code could have a deadlock once the output grew large enough.
(So of course it would pass tests, but fail occasionally in production.)
Suppose that the messages these two children exchanged with each other were
synchronous somehow, such that blocking one child would eventually block the
other. And suppose that both children had enough output that they could also
block if the parent didn't clear space in their stdout pipe buffers by reading.
The call to `handle1.wait` would block until `child1` was finished. Then
`child2` would block writing to stdout, because the parent wouldn't be reading
it yet. And then `child1` would block on `child2`, waiting for messages. That
would be a deadlock, and it would probably be difficult to reproduce and debug.

For this reason, the `start` method must use threads to supply input and
capture output. That guarantees that the parent will never cause its children
to block on output, regardless of its order of operations after `start`.

## Killing grandchild processes?

**Currently unsolved.** This is something of a disaster area in Unix process
management. Consider the following two scripts. Here's `test1.py`:

```python
import subprocess
subprocess.run(["sleep", "100"])
```

And here's `test2.py`:

```python
import subprocess
import time
p = subprocess.Popen(["python", "./test1.py"])
time.sleep(1)
p.kill()
p.wait()
```

That is, `test1.py` starts a `sleep` child process and then waits on it. And
`test2.py` starts `test1.py`, waits for a second, and then kills it. The
question is, if you run `test2.py`, what happens to the `sleep` process? If
you look at something like `pgrep sleep` after `test2.py` exits, you'll see
that `sleep` is _still running_. Maybe that's not entirely surprising, since we
only killed `test1.py` and didn't explicitly kill `sleep`. But compare that to
what happens if you start `test2.py` and then quickly press Ctrl-C. In that
case, `sleep` is killed. What the hell!

What's going on is that there's a difference between signaling a process ID and
signaling a [process group ID](https://en.wikipedia.org/wiki/Process_group).
The `kill` function in Python (and Bash and pretty much every other language)
does the former, which only kills a single process. Ctrl-C in the shell does
the latter, which kills a whole tree of child processes at once. Process group
signaling is a great way to cancel an "entire job" reliably, even if that job
has spawned more child processes. So why do existing `kill` functions use the
surprisingly weak sauce that is individual process signaling?

The sad truth is that process group signaling basically only works for shells.
When the shell forks a child process, before it calls `exec`, it calls
`setpgid` to set a new process group ID. Because child processes typically do
_not_ call `setpgid` themselves, the child process and all of its transitive
children end up in the same process group (which typically has a group ID equal
to the process ID of the original child). However, if one of those child
processes _does_ call `setpgid`, the relationship between it and the other
children gets lost. Ctrl-C and Ctrl-Z stop working properly. The fundamental
issue is that each process only has a single process group ID. Process groups
do not form a tree.

What does form a tree, however, is process IDs themselves. Each process knows
the ID of its parent, so it's possible to query a process's full transitive
tree of children. The problem with using such a query for signaling purposes is
that it's racy. In the time between when you run the query and when you send
signals, any process in the tree may have spawned new children. (Even worse,
some processes might've exited, and those PIDs might've been reused for
processes that aren't in the tree.) We can _just barely almost_ solve that
problem by killing a child process, not reaping it yet, and querying the child
processes of the zombie. But alas, that strategy only works for one level of
the tree, as the OS automatically reaps any zombie whose parent is also a
zombie. So close!

The modern solution for all of this on Linux is supposed to be
[cgroups](https://en.wikipedia.org/wiki/Cgroups). But as if to rub salt in our
wounds, it turns out there's [no way to atomically signal a
cgroup](https://jdebp.eu/FGA/linux-control-groups-are-not-jobs.html). Systemd
works around this problem with a kill loop that repeatedly queries the PIDs in
a cgroup and tries to kill all of them individually. And it's _still_
vulnerable to the PID reuse race. Solving those races might be possible by
abusing SIGSTOP (you better hope you're the only part of the system possibly
sending SIGCONT to unrelated processes) or something called "the freezer",
though if Systemd doesn't attempt to use those techniques that's a pretty bad
sign.

Linux is in the middle of adding new APIs like
[`pidfd_send_signal`](https://lwn.net/Articles/794707/), but none of them are
aimed at improving the situation with grandchildren. Windows has a cleaner
solution ([job
objects](https://docs.microsoft.com/en-us/windows/win32/procthread/job-objects)),
but even there it sounds like some important features aren't supported on
Windows 7. Realistically, there won't be good techniques for Duct to use to
solve this problem for many years.
