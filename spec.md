# Notes for implementers

Duct was designed for both Python and Rust, and the hope is that it can be
cloned in lots of different languages. To help with that, this document
clarifies how duct handles a number of different corner cases.

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
the current directory, but Windows will accept the bare name. In duct we mostly
just go with the flow on these conventions, by passing string arguments
straight through to the OS.

However, when the program name is given as an explicit path type (like
`pathlib.Path` in Python), duct guarantees that it will behave like a filepath.
To make this work, when we stringify path objects representing relative paths,
we join a leading `.` if it's missing. This solves two problems:

- It prevents "command not found" errors on Posix for paths to programs in the
  current dir. This is especially important in Python, where the native path
  type actively strips out dots.
- It prevents paths to a nonexistent local file, which _should_ result in
  "command not found", from instead matching a program in the `$PATH`.

Note that this applies to `sh` in addition to `cmd`. Although it's tempting to
have `sh` accept only strings and not paths, it's important to be able to
execute paths in shell mode on Windows, where scripts have no Unix-style
shebang and instead rely on the shell to figure out what their interpreter
should be.

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
canonicalize relative exe paths (in `cmd` only, not in `sh`) when the `dir`
method is in use.

## Picking a shell

Implementations should follow [Python's lead
here](https://docs.python.org/3/library/subprocess.html#popen-constructor). Use
`/bin/sh` on POSIX systems and whatever's in the `COMSPEC` environment variable
on Windows.

## Inheritable pipes on Windows

Spawning child processes on Windows usually involves duplicating some pipes and
making them inheritable. Unfortunately, that means that *any* child spawned on
other threads while those pipes are alive will inherit them
(https://support.microsoft.com/kb/315939). Even if a given duct implementation
doesn't use threads internally, it might get called from multiple threads at
the same time. Duct implementations need to either make sure that the standard
library they're built on uses a mutex to prevent bad inheritance ([as Rust
does](https://github.com/rust-lang/rust/blob/1.14.0/src/libstd/sys/windows/process.rs#L169-L179)),
or use their own mutex internally as best effort ([as we do in
Python](https://github.com/oconnor663/duct.py/blob/0.5.0/duct.py#L676-L686)).
