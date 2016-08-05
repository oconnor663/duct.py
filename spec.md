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

## Picking a shell

Implementations should follow [Python's lead
here](https://docs.python.org/3/library/subprocess.html#popen-constructor). Use
`/bin/sh` on POSIX systems and whatever's in the `COMSPEC` environment variable
on Windows.
