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
* [Cleaning up zombie children](#cleaning-up-zombie-children)
* [Making `kill` thread-safe](#making-kill-thread-safe)
* [Adding `./` to program names given as relative paths](#adding--to-program-names-given-as-relative-paths)
* [Preventing `dir` from affecting relative program paths on Unix](#preventing-dir-from-affecting-relative-program-paths-on-unix)
* [Preventing pipe inheritance races on Windows](#preventing-pipe-inheritance-races-on-windows)
* [Matching platform case-sensitivity for environment variables](#matching-platform-case-sensitivity-for-environment-variables)
* [Using IO threads to avoid blocking children](#using-io-threads-to-avoid-blocking-children)
* [Killing grandchild processes?](#killing-grandchild-processes)

## Reporting errors by default

Most programming languages make error checking the default, either by crashing
your program with an exception, or by emitting warnings or compiler errors for
unchecked results. But the child process APIs in most standard libraries
(including Python and Rust) do the opposite, ignoring non-zero exit statuses by
default. That's unfortunate, because many command line utilities helpfully
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

## Cleaning up zombie children

On Unix platforms (but not Windows) child processes hold some OS resources even
after they exit, until their parent process waits on them and receives their
exit status. The OS will do this cleanup automatically if the parent exits, but
not as long as the parent is alive. These exited-but-un-"reaped" children are
called [zombie processes](https://en.wikipedia.org/wiki/Zombie_process), and
they're a common type of resource leak if you run child processes in the
background (`start` as opposed to `run`).

The Python [`subprocess`](https://docs.python.org/3/library/subprocess.html)
module mitigates this by [keeping a global list of leaked child
processes](https://github.com/python/cpython/blob/v3.13.3/Lib/subprocess.py#L1133-L1146)
and polling each of them [whenever it's about to spawn a new child
process](https://github.com/python/cpython/blob/v3.13.3/Lib/subprocess.py#L832).
The Rust implementation of Duct uses the same strategy. The downside of this
strategy is that it makes process spawning O(n<sup>2</sup>) in the worst case,
if the caller leaks lots of long-lived child processes. Children don't enter
the global list as long you retain a `Handle`, so most applications won't hit
this case.

An alternative could be to spawn a waiter thread for each leaked child, but
that's more expensive in the common case, and also spawning a thread can fail.
It would be better to share a global waiter thread, but the historical options
for implementing something like that (`SIGCHLD` or `waitpid(-1)`) are
off-limits to library code that doesn't own the whole parent process. Polling
Linux [`pidfd`](https://www.corsix.org/content/what-is-a-pidfd)s might be the
best modern option, but that API is still new by kernel standards (2019), and
most other Unix platforms have no equivalent.

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

As part of a best-effort check for this bug, Python 3.9 [changed the
behavior](https://bugs.python.org/issue38630) of `Popen.kill` to reap child
processes that have already exited. That [interacts
poorly](https://github.com/oconnor663/duct.py/commit/5dfae70cc9481051c5e53da0c48d9efa8ff71507)
with code that calls `os.waitid` or `os.waitpid` directly.

## Adding `./` to program names given as relative paths

When you run the command `foo`, it can be ambiguous whether you mean `./foo` in
current directory or e.g. `/usr/bin/foo` in the `PATH`. Different platforms do
different things here: Unix-like platforms usually require the leading `./` for
programs in the current directory, but Windows will accept a bare filename.
Duct defers to the platform for interpreting program names that are given as
strings, but it prepends `./` to program names that are given as path types
(`pathlib` in Python, `std::path` in Rust) when the path is relative.

This solves two problems:

- It prevents "command not found" errors on Unix-like platforms for paths to
  programs in the current directory. This is especially important in Python,
  where `pathlib.Path` automatically strips leading dots.
- It prevents paths to a nonexistent local file, which _should_ result in
  "command not found", from instead matching a program in the `%PATH%` on
  Windows.

Note that Rust 1.58 [changed the
behavior](https://blog.rust-lang.org/2022/01/13/Rust-1.58.0.html#reduced-windows-command-search-path)
of `std::process::Command` to exclude the current directory from the search
path on Windows.

## Preventing `dir` from affecting relative program paths on Unix

Windows and Unix take different approaches to setting a child's working
directory. The `CreateProcess` function on Windows has an explict
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

## Using IO threads to avoid blocking children

When input bytes are supplied or output bytes are captured, Duct's `start`
method uses background threads to do IO, so that IO makes progress even if
`wait` is never called. Duct's `reader` method doesn't use a thread for
standard ouput, since that's left to the caller, but it still uses background
threads to supply input bytes or to capture standard error.

Consider the following scenario. You want to spawn two child processes that
will talk to each other somehow, for example using the local network. You also
want to capture the output of each process. Your code might look like this:

```python
handle1 = cmd("child1").stdout_capture().start()
handle2 = cmd("child2").stdout_capture().start()
output1 = handle1.wait().stdout
output2 = handle2.wait().stdout
```

If Duct handled captured output without threads, e.g. using a read loop inside
of `wait`, that code could have a deadlock once the output grew large enough.
(So of course it would pass tests but fail occasionally in production.) Suppose
that the messages the children exchanged with each other were synchronous
somehow, such that blocking one child would eventually block the other. And
suppose that both children had enough output that they could also block if the
parent didn't clear space in their stdout pipe buffers by reading. The call to
`handle1.wait` would block until `child1` was finished. Then `child2` would
block writing to stdout, because the parent wouldn't be reading it yet.
Finally, `child1` would block on `child2`, waiting for messages. That would be
a deadlock, and it would probably be difficult to reproduce and debug.

For this reason, the `start` method must use threads to supply input and
capture output. That guarantees that the parent will never cause its children
to block on output, regardless of its order of operations after `start`.

Also, note that observing that a child process has exited does not guarantee
that its IO pipes will close or that any IO threads using those pipes will
exit. If the child process spawns any grandchild processes (more on those
below), the grandchildren usually inherit copies of the child's IO pipes, and
they can outlive the child and keep those pipes open indefinitely. Non-blocking
methods like
[`Handle.poll`](https://ductpy.readthedocs.io/en/latest/#duct.Handle.poll) in
Python or
[`Handle::try_wait`](https://docs.rs/duct/latest/duct/struct.Handle.html#method.try_wait)
in Rust need to explicitly check whether IO threads have exited before doing
any blocking joins.

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
question is, if you run `test2.py`, what happpens to the `sleep` process? If
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
[cgroups](https://en.wikipedia.org/wiki/Cgroups). ~~But as if to rub salt in
our wounds, it turns out there's [no way to atomically signal a
cgroup](https://jdebp.eu/FGA/linux-control-groups-are-not-jobs.html). Systemd
works around this problem with a kill loop that repeatedly queries the PIDs in
a cgroup and tries to kill all of them individually. And it's _still_
vulnerable to the PID reuse race.~~ **Update: As of Linux 5.14 (August 2021)
cgroups support an atomic `cgroup.kill` operation that looks robust. The last
major holdout might be macOS.**

Windows has a cleaner solution ([job
objects](https://docs.microsoft.com/en-us/windows/win32/procthread/job-objects)),
but even there it sounds like some important features aren't supported on
Windows 7. Realistically, there won't be good techniques for Duct to use to
solve this problem for many years.
