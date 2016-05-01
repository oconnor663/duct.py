# Notes for implementers

## SIGPIPE

Implementations need to catch broken pipe errors in input writer threads, so
that it's not an error for a subprocess to ignore its input. (This usually only
shows up when the input is larger than the OS pipe buffer, ~66KB on Linux, so
that the writing thread blocks.)

Many languages (including Python and Rust) install signal handlers for SIGPIPE
by default, so that broken pipe errors can go through the usual
exception/result mechanism rather instead of killing the whole process.
Implementations in languages that don't (C++?) will need to figure out what the
heck to do about this.

## Executing a local script from a path object

It should be possible to invoke `./foo.sh` using the native path type. This
works fine in Rust, but it's tricky in Python, because `Path("./foo.sh")`
stringifies to `"foo.sh"`, and invoking that is an error (assuming "foo.sh" is
present only in the current directory, and not in the $PATH). The Python
implementation works aroud this by `path.join`ing a leading `.` onto any
non-absolute path after stringifying it. Ideally implementations that need this
sort of workaround should preserve the usual OS semantics for non-path strings,
so that `cmd("foo.sh")` is still an error.

Note that what counts as an "absolute path" can be subtle on Windows. In
particular, `\foo\bar.txt` is usually *not* considered absolute, because it
doesn't include a drive letter (e.g. `C:\foo\bar.txt`). When doing the
join-leading-dot workaround, implementations should avoid adding dots to these
almost-absolute paths. That said, many path join implementations will do the
right thing here and ignore the dot.

Although it's tempting to have sh() accept only strings and not paths, it's
important to be able to execute paths in shell mode on Windows, where scripts
have no unix-style shebang and instead rely on cmd.exe to figure out what their
interpreter should be. That means sh() also needs to observe the note above
about paths that start with dot.
