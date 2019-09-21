# Gotchas, bugs, and platform inconsistencies

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

## Reporting errors by default

Most programming languages make error checking the default, either by crashing
your program with an exception, or by emitting warnings or compiler errors for
unchecked results. However, the child process APIs in most standard libraries
(including Python and Rust) do the opposite, ignoring non-zero exit statuses by
default. That's unforunate, because most command line utilities helpfully
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
reading all of its input. Most language standard libraries get this right.

Notably on Unix, this requires the process to suppress `SIGPIPE`.
Implementations in languages that don't suppress `SIGPIPE` by default (C/C++?)
have no choice but to set a signal handler from library code, which might
conflict with application code or other libraries. There is no good solution to
this problem.

## Waiting on killed children by default

Many languages (Python, Rust, Go) provide a `kill` API that sends `SIGKILL` to
a child process. The caller is then required to call `wait` or similar on the
child, or else the child may become a zombie and leak resources. Duct performs
the `wait` by default instead.

`SIGKILL` cannot be caught or ignored, and so waiting will almost always return
quickly. One of the rare exceptions to this is if the process is stuck in an
uninterruptible system call, for example a `read` from an unresponsive FUSE
filesystem. In general, Duct's correctness priorities are:

1. Do not leave zombie children or leak other resources.
2. Do not block in a non-blocking API (`start`, `reader`, `try_wait`, or
   `kill`).
3. Do not let errors pass silently.

In this case #1 takes priority over #2.

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
approach on Unix-like platforms. Windows does not have this issue.

## Adding `./` to program names that are explicitly given as relative paths

When we run the command `foo`, it can be ambiguous whether we mean `./foo` in
current directory or something like `/usr/bin/foo`. Different platforms do
different things here: Unix-like platforms usually require the leading `./` for
programs in the current directory, but Windows will accept a bare filename.
Duct defers to the platform for interpreting program names given as strings,
but it explicitly prepends `./` to program names given as explicit path types
(`pathlib` types in Python, `std::path` types in Rust) when the path is
relative.

This solves two problems:

- It prevents "command not found" errors on Unix-like platforms for paths to
  programs in the current directory. This is especially important in Python,
  where `pathlib.Path` automatically strips leading dots.
- It prevents paths to a nonexistent local file, which _should_ result in
  "command not found", from instead matching a program in the `%PATH%` on
  Windows.

## Preventing `dir` from affecting relative program paths

Windows and Unix take different approaches to setting a child's working
directory. The `CreateProcess` function on Windows takes an explict path
argument, while most Unix platforms require a `chdir` in between `fork` and
`exec`. Unfortunately, those two approaches give different results when you
have a _relative path_ to the child executable. On Windows the relative path is
interpreted from the parent's working directory. But on Unix, it's interpreted
relative to the child's.

The Windows behavior is preferable, because it lets you add a `dir` argument
without breaking any existing relative program paths. Duct provides this
behavior on all platforms, by absolutifying relative program paths on Unix-like
platforms when the `dir` method is in use.

## Preventing a pipe inheritance race condition on Windows

Spawning child processes on Windows usually involves duplicating some pipes and
making them inheritable. Unfortunately, that means that *any* child spawned on
other threads while those pipes are alive will inherit them
(https://support.microsoft.com/kb/315939). Even if a given Duct implementation
doesn't use threads internally, it might get called from multiple threads at
the same time. The Rust standard library [has an internal
mutex](https://github.com/rust-lang/rust/blob/1.14.0/src/libstd/sys/windows/process.rs#L169-L179)
to prevent this race, but the Python standard library [does
not](https://bugs.python.org/issue24909). In Python on Windows, Duct uses its
own internal mutex to prevent this race. That doesn't prevent racing with other
libraries, but it at least means that multiple Duct callers on different
threads are protected.

## Matching platform case-sensitivity for environment variables

Environment variable names are case-sensitive on Unix but case-insensitive on
Windows. Duct defers to the platform when setting variables. However, methods
like `env_remove` require keeping an internal map of variables, and map keys
are always case-sensitive. When managing that map, Duct follows the platform's
rule, which means upper-casing names on Windows.

## Cleaning up a partially started pipeline

If the left half of a pipeline starts successfully, but the right half fails to
start, Duct **kills and awaits** the left half, and then returns the error from
the right half.

To be clear, "fails to start" does not mean "exits with a non-zero status".
Rather, this is the situation where the child never starts at all. There is no
exit status, because there is no child process. Most commonly that means a
command name was misspelled, or the target executable is missing. Less
commonly, the system may be under heavy load and failing to spawn new
processes.

Frankly, this sucks. It's bad behavior to kill child processes without being
asked to by the caller. An unexpected kill signal might cause some programs to
misbehave or corrupt data. But recall Duct's correctness priorities:

1. Do not leave zombie children or leak other resources.
2. Do not block in a non-blocking API (`start`, `reader`, `try_wait`, or
   `kill`).
3. Do not let errors pass silently.

Leaving the left half of the pipeline running would violate #1. If the child
failed to start because the system is under heavy load, leaking resources might
exacerbate the problem and prevent recovery. Waiting on the left half to exit
on its own would violate #2. Deferring error reporting until the caller waits
would violate #3.

Killing child processes isn't good, but it's the least bad option in a bad
situation. Ultimately, a correct program will only encounter this behavior when
the system it's running on is suffering from resource exhaustion. In that case,
it *already* needs to be prepared for random kill signals, like from the Linux
OOM killer. This policy makes it easier to write correct programs, without
creating any new problems that a correct program doesn't already have.

## Using IO threads to avoid blocking children

When input bytes are supplied or output bytes are captured, Duct's `start`
method uses background threads to do IO, so that IO makes progress even if
`wait` is never called. Duct's `reader` method doesn't use a thread for
standard ouput, since that's left to the caller, but it still uses background
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

That code is correct. However, if Duct handled captured output without threads,
that code could have a deadlock once the output grew large enough. (So of
course it would pass tests, but fail occasionally in production.) Suppose that
the messages these two children exchanged with each other were synchronous
somehow, such that blocking one child would eventually block the other. And
suppose that both children had enough output that they could also block if the
parent didn't clear space in their stdout pipe buffers by reading. The call to
`handle1.wait` would block until `child1` was finished. Then `child2` would
block writing to stdout, because the parent wouldn't be reading it yet. And
then `child1` would block on `child2`, waiting for messages. That would be a
deadlock, and it would be very difficult to reproduce or debug.

For this reason, the `start` method must use threads to supply input and
capture output. That guarantees that the parent will never cause its children
to block, regardless of its order of operations after `start`.
