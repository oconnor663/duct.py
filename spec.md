# Notes for implementers

Duct was designed for both Python and Rust, and the hope is that it can be
cloned in lots of different languages. To help with that, this document
clarifies how Duct handles a number of different corner cases.

## SIGPIPE

Implementations need to catch broken pipe errors in input writer threads, so
that it's not an error for a subprocess to ignore its input. (This usually only
shows up when the input is larger than the OS pipe buffer, ~66KB on Linux, so
that the writing thread blocks.)

Many languages (including Python and Rust) install signal handlers for SIGPIPE
by default, so that broken pipe errors can go through the usual
exception/result mechanism instead of killing the whole process.
Implementations in languages that don't (C++?) will need to figure out what the
heck to do about this.

## Ambiguity between the $PATH and the current directory

When we run the command "foo", it's ambiguous whether we mean "foo" somewhere
in the `$PATH`/`%PATH%` or "foo" in current directory. Different OS's do
different things here: Posix usually requires a leading `./` for programs in
the current directory, but Windows will accept the bare name. In Duct we mostly
just go with the flow on these conventions, by passing string arguments
straight through to the OS.

However, when the program name is given as an explicit path type (like
`pathlib.Path` in Python), Duct guarantees that it will behave like a filepath.
To make this work, when we stringify path objects representing relative paths,
we join a leading `.` if it's missing. This solves two problems:

- It prevents "command not found" errors on Posix for paths to programs in the
  current dir. This is especially important in Python, where the native path
  type actively strips out dots.
- It prevents paths to a nonexistent local file, which _should_ result in
  "command not found", from instead matching a program in the `$PATH`.


## Consistent behavior for `dir`

Windows and Unix take different approaches to setting a child process's cwd.
The `CreateProcess` function on Windows takes a directory argument natively,
while most Unix implementations do a `chdir` in between `fork` and `exec`.
Unfortunately, those two approaches give different results when you have a
_relative path_ to the child executable. On Windows the relative path is
interpreted from the parent's cwd, but on Unix, because `chdir` happens before
`exec`, it's interpreted relative to the child's.

The Windows behavior is preferable, because it keeps the exe and cwd paths
independent of each other, rather than making the caller remember the
interaction between them. To guarantee that behavior, implementations need to
canonicalize relative exe paths when the `dir` method is in use.

## Inheritable pipes on Windows

Spawning child processes on Windows usually involves duplicating some pipes and
making them inheritable. Unfortunately, that means that *any* child spawned on
other threads while those pipes are alive will inherit them
(https://support.microsoft.com/kb/315939). Even if a given Duct implementation
doesn't use threads internally, it might get called from multiple threads at
the same time. Duct implementations need to either make sure that the standard
library they're built on uses a mutex to prevent bad inheritance ([as Rust
does](https://github.com/rust-lang/rust/blob/1.14.0/src/libstd/sys/windows/process.rs#L169-L179)),
or use their own mutex internally as best effort ([as we do in
Python](https://github.com/oconnor663/duct.py/blob/0.5.0/duct.py#L676-L686)).

## Supporting kill and wait at the same time

On Unix (though not Windows) there's a race condition between `kill` and
`waitpid`. If a process exits right before you signal it, a waiting thread
might clean it up and free its PID, and an unrelated process could immediately
reuse that PID. It's not likely, but all of that could happen before the
waiting thread has a chance to make a note of it, and so the killing thread
might end up killing that unrelated process. This race condition is why the
Rust standard library [doesn't allow shared access to child
processes](https://doc.rust-lang.org/std/process/struct.Child.html#method.kill).

It's possible to avoid this race however, using a newer POSIX function called
`waitid`. That function has a `WNOWAIT` flag that leaves the child in its
zombie state, so that its PID isn't freed for reuse. That gives the waiting
thread a chance to block further kills, before cleaning up the child properly.
The [`shared_child` crate](https://github.com/oconnor663/shared_child.rs) is an
example implementation using `waitid`.

Duct implementations should prefer this strategy over plain `waitpid`, for two
reasons. First, shared access is a nice feature. But more importantly,
languages other than Rust aren't very good at preventing shared access. It's
much better to make the library safe, than to hope the user reads the docs
about the ways its unsafe.

Another way to prevent this race would be to use only nonblocking waits, so
that kill and wait could take the same locks. However, that requires one of two
approaches: either we'd need to listen for `SIGCHLD` to know when a child has
exited, or we'd need to wait and sleep in a loop. The signal approach assumes
we own the current process's signal handlers, which isn't always true. (If
Python calls out to a Rust library, for example, the Rust code can't set signal
handlers without unsetting Python's.) The sleep loop approach works for most
cases, using short sleeps that grow over time, but it causes a potentially long
delay after the end of a long-running child, which isn't always acceptable.
Frequent wakeups can also hurt battery life. The `waitid` approach above avoids
these problems.

## Case-insensitive environment variables on Windows

Environment variables on Windows are added and deleted in a case-insensitive
way. We usually don't care about it this *until* we copy the entire environment
into a dictionary. *Then* we will find that the keys in our dictionary have
been uppercased (probably), and trying to edit or delete them with their
lowercase names no longer has the right effect. The right thing to do here will
depend on the specifics of each language and how it talks to the environment,
but the core requirement is that something like this must work:

```python
import os
# Set a lowercase variable in the parent environment.
os.environ["foo"] = "bar"
# Run a Duct command that clears that same variable.
# This command MUST NOT see the variable "foo" or (on Windows) "FOO".
cmd("my_cmd.sh").env_remove("foo").run()
```
