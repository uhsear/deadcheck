# deadcheck

Prove that every check in a check registry can fire. It replaces one registered check at a time
with a stub that returns nothing, runs your tests, and says which checks no test needs.

A validation run had a registry of checks, each one hunting for one kind of defect in address and
street data. Many checks had only a test that fed them clean data and expected no findings. The
suite was green. A review found that replacing the body of such a check with `return []` would have
passed every test, and that three registered checks could never fire. The clean-sample test passes
for a check that works and for a check that is dead, so nothing had ever been red.

A stubbed check is the cheapest test of that. This tool does it for every id in the registry:

```
$ python deadcheck.py --registry checks:CHECKS -- -m unittest
deadcheck: registry checks:CHECKS holds 4 check(s), 4 selected, stub value []
baseline: the test command passed with no stub
PROVEN       LEN-01  stub called 2 time(s) and the tests failed (exit 1)
SURVIVED     NUL-01  stub called 1 time(s) and the tests still passed: a test runs this check and asserts nothing about what it found
UNCALLED     DUP-01  the tests passed and never called the stub: no test runs this check
PROVEN       RNG-01  stub called 1 time(s) and the tests failed (exit 1)
checks stubbed: 4 of 4
  PROVEN       2
  SURVIVED     1
  UNCALLED     1
  SUSPECT      0
  TIMEOUT      0
  UNKNOWN      0
  UNSTUBBABLE  0

VERDICT: 2 of 4 check(s) proven able to fire, 2 not proven: NUL-01, DUP-01. A check no test needs to fire may be unable to fire.
```

`NUL-01` has a test, and the test only feeds it clean data. `DUP-01` has no test. Both would sit
in a green suite for years. The project behind this run is listed under
[The fixture](#the-fixture), so you can run it yourself.

One Python file, standard library only, Python 3.9 or later. It runs the same on Windows and on
Linux. The tool writes its report only when you pass `--apply`. It also uses one unnamed
temporary file for the output of each test run. Your test command runs as it always does and may
write files of its own, such as the `.pytest_cache` folder that `pytest` makes.

## What already exists

The tools for this question in general are mutation testers. They are much more thorough than
this one, and they are the right choice when you want to know whether your tests catch small
changes anywhere in a program.

- **[mutmut](https://github.com/boxed/mutmut)** (BSD-3-Clause). Its README says it "remembers work
  that has been done, so you can work incrementally", "knows which tests to execute", and
  "found mutants can be applied on disk with a simple command". It needs `fork`: "if you want to run on windows, you must run
  inside WSL".
- **[Cosmic Ray](https://github.com/sixty-north/cosmic-ray)** (MIT). "It makes small changes to
  your source code, running your test suite for each one." Its README shows a badge for Python 3.9
  and later on Windows. I did not install or run it.
- **[mutatest](https://github.com/EvanKepner/mutatest)** and
  **[MutPy](https://github.com/mutpy/mutpy)** do the same kind of work. mutatest says it was
  tested on Linux, Windows and macOS.

None of these was installed or run for this README. What each one says is quoted from its own
README, which was read again on 2026-10-29.

This tool asks one narrower question and keeps the answer per check id. A mutation tester mutates
operators and reports the mutants that survive. It does not know that your code has a registry. Here the
unit is the registered check, and the answer is one of seven named statuses for each id, with an
exit code a scheduled job can read. One stub per check also costs one test run per check, not one
per mutant.

A search of GitHub on 2026-10-29 (`gh search repos deadcheck`) found other repositories with
this name. They do other work: a dead man's switch service, a dead-code finder for JavaScript, a
dead link finder for documentation, and a tool that scores a grading rubric. A search for
`mutation testing` with the language set to Python listed mutmut first, Cosmic Ray second, MutPy
fifth and mutatest twelfth among the first 15 results. Searches for a registry-of-checks tool
found nothing. This is an absence of results in a few searches, not a claim that nothing else
exists.

## Requirements

- Python 3.9 or later. No other package. `--self-test` ran green on 3.9.25, 3.10.20, 3.12 and 3.13
  on Windows, and on 3.12 on Linux, with the same assertion count on every one. Python 3.11,
  3.14 and macOS were not run.
- A test command that Python can run: `-m module args` or `script.py args`. `unittest` and a plain
  script are exercised by the self-test. `pytest` was run by hand, see Experiment 3.
- A registry: a `dict` of id to function, a list of functions (the function name is the id), or
  a list of `(id, function)` pairs. Any other shape is refused with exit 3. When the container is
  right and one item is wrong, the message names the item: an id that is not a string, a value
  that is not callable, a tuple that is not a pair, an item with no name. The self-test pins each
  one.

## Usage

```
python deadcheck.py --registry MODULE:NAME [options] -- TEST COMMAND
python deadcheck.py --self-test
```

| Option | What it does |
|---|---|
| `--registry MODULE:NAME` | Where the checks are. `NAME` may be dotted. Required. |
| `--cwd DIR` | Folder to run in and to import from. Default `.`. |
| `--python EXE` | The Python that runs the tests. Default: the one running this tool. A path with a folder in it is made absolute first. |
| `--stub-value LITERAL` | What a stub returns, as a Python literal. Without it the stub returns `[]`, or the empty value of the type the check really returns. Code is refused. |
| `--only ID` | Stub only this check. Repeat to name more. |
| `--allow ID` | A check that may stay unproven without failing the run. Repeat to name more. |
| `--timeout SEC` | Seconds one test run may take. Default 600. Must be finite and above zero; use `1e9` for no limit. |
| `--tail N` | Print the last N lines of the test output under a SUSPECT or UNKNOWN check. |
| `--out FILE` | The JSON report. A new file in a folder that exists. Needs `--apply` to be written. |
| `--apply` | Write `--out`. The tool writes no report without it. |
| `--self-test` | Run the offline assertions and exit. |

The parser has `allow_abbrev=False`. A prefix such as `--ap` is an error, not `--apply`. The
self-test pins five prefixes, and the `--apply` one is marked as a pinned defect.

### The seven statuses

| Status | The stub was called | The tests | Meaning |
|---|---|---|---|
| `PROVEN` | yes | failed, and passed with the real check | A test fails when the check returns its empty value. |
| `SURVIVED` | yes | passed | A test runs the check and asserts nothing about what it found. |
| `UNCALLED` | no | passed | No test runs the check. |
| `SUSPECT` | no, or yes | failed | The failure is not what the check returned. It proves nothing. |
| `TIMEOUT` | | ran past `--timeout` | No verdict. |
| `UNKNOWN` | not reported | ended early | The child ended before it said whether it called the stub. |
| `UNSTUBBABLE` | | not run | An `async` function, a callable object, a builtin or a partial. |

Only `PROVEN` counts. The order of the tests in the code matters: a timeout outranks the hits it
reported before it was cut off, and a failed run with no hits is `SUSPECT`, never `PROVEN`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every selected check is `PROVEN` or named in `--allow`. |
| 1 | At least one check is not proven. |
| 2 | A process could not be started, or the report could not be written. |
| 3 | Refused: the baseline run fails, the registry cannot be loaded, or two ids share an id or a function. |
| 4 | Nothing was checked. An empty registry is not a clean result. |
| 64 | Usage error. This includes the exit code 2 that `argparse` uses. |
| 70 | An unexpected crash in the tool. It is never a finding. |

Code 64 exists because 2 means an I/O failure here, and 1 means a check was not proven. A
scheduled job must be able to tell those apart.

With `--only`, exit 0 covers only the checks you picked. The verdict line then ends with
`Only 1 of 4 registered check(s) were selected.` Do not pass `--only` in a gate that must cover
the whole registry.

## How it works

1. A child Python process imports the registry and lists the ids. The parent refuses a registry
   with a repeated id, or with two ids on one function.
2. A baseline run of your tests, with no stub. If it fails, the tool stops with exit 3. A failure
   under a stub would prove nothing.
3. For each id, a fresh child process imports the registry, replaces the code object of that one
   function with a stub, and runs your tests by `runpy`. The stub counts its calls and returns the
   literal. The child prints the count on a marker line.
4. The parent reads the exit code and the count, and picks the status.
5. A run that looks `PROVEN` is checked twice more, as the next section says.

Replacing `__code__` means a test that imports the function directly is stubbed as well as a test
that goes through the registry. The tool uses no fork and no signal, which is why it runs on
Windows. The stub takes the same parameters as the check (positional-only, keyword-only,
`*args`, `**kwargs`), and the function keeps its name, docstring and defaults, so a test that
reads the signature still passes. The decision core (`classify`, `verdict`, `explain`,
`shared_ids`, `fit_stub` and the parsers) takes values and returns values. The processes, the
files and the printing are in separate functions.

The child process loads this file by its path, not by its name, and it drops the current folder
from its import path first. The file may be called `dead-check.v2.py`, and a project file called
`json.py` does not replace the standard library inside the tool. The child's output goes to an
unnamed temporary file, not to a pipe, so `--timeout` ends the run even when a test left a helper
process running. The child also gets `PYTHONIOENCODING=utf-8:replace`, so a test that prints a
character the Windows console code page cannot encode does not die for that reason. A test exit
status that is a multiple of 256 would read as 0 on Linux, so the tool turns it into 1.

### A failure is not always the check's

A stub that makes the tests fail is only proof when the stub's return value caused the failure.
Three things can fail a run for another reason, and each one used to read as `PROVEN`.

- **A flaky or stateful suite.** A run that looks `PROVEN` is repeated with the real check passed
  through. If the tests fail again, the status is `SUSPECT` and the line says the failure is not
  what the check returned. Self-test: *a suite that fails on every run after the first does not
  turn a dead check PROVEN*.
- **A different signature.** The stub repeats the check's parameters. A contract test that reads
  `inspect.signature` of every registered check no longer fails because of the stub. Self-test:
  *a test that reads the signature of a check does not turn a dead check PROVEN*.
- **A return type that is not a list.** The passed-through run records the type of what the check
  really returns. A check that returns a `dict`, a `tuple`, a `set`, a `str`, `bytes` or `None` is
  stubbed again with the empty value of that type. A check that returns anything else has no empty
  value the tool can guess, so it is `SUSPECT` and the line says to pass `--stub-value`. An
  explicit `--stub-value` is always obeyed and never replaced.

## The fixture

Experiments 1 to 3 use this project: four checks in a registry built with a decorator, so each
registered function is a `functools.wraps` wrapper. Save the two files in one folder.

`checks.py`:

```python
import functools

CHECKS = {}


def register(check_id):
    def wrap(fn):
        @functools.wraps(fn)
        def wrapper(rows):
            return fn(rows)
        CHECKS[check_id] = wrapper
        return wrapper
    return wrap


@register("LEN-01")
def name_too_long(rows):
    return [r for r in rows if len(r.get("name", "")) > 30]


@register("NUL-01")
def name_blank(rows):
    return [r for r in rows if r.get("name") == ""]


@register("DUP-01")
def id_repeated(rows):
    seen, found = set(), []
    for r in rows:
        if r["id"] in seen:
            found.append(r)
        seen.add(r["id"])
    return found


@register("RNG-01")
def number_out_of_range(rows):
    return [r for r in rows if not 1 <= r.get("number", 1) <= 99999]
```

`test_checks.py`:

```python
import unittest

import checks

GOOD = [{"id": 1, "name": "Main St", "number": 10}, {"id": 2, "name": "Oak Ave", "number": 20}]


class Tests(unittest.TestCase):
    def test_len_fires(self):
        bad = GOOD + [{"id": 3, "name": "x" * 31, "number": 5}]
        self.assertEqual([r["id"] for r in checks.CHECKS["LEN-01"](bad)], [3])

    def test_len_clean(self):
        self.assertEqual(checks.CHECKS["LEN-01"](GOOD), [])

    def test_nul_clean_only(self):
        self.assertEqual(checks.CHECKS["NUL-01"](GOOD), [])

    def test_rng_fires(self):
        bad = GOOD + [{"id": 4, "name": "Elm St", "number": 0}]
        self.assertEqual([r["id"] for r in checks.CHECKS["RNG-01"](bad)], [4])


if __name__ == "__main__":
    unittest.main()
```

## Experiments

The outputs below are from real runs of this tool on that fixture, on 2026-10-29.

### Experiment 1: a suite with clean-sample tests

The run in the opening is this one. `LEN-01` and `RNG-01` have a test that feeds them a bad row.
`NUL-01` has a test that feeds it good rows. `DUP-01` has none. Same result on Windows (Python 3.13)
and on Linux (Python 3.12): `2 of 4 ... not proven: NUL-01, DUP-01`, exit 1.

### Experiment 2: add the missing tests

Add these two tests to the class in `test_checks.py`, before the `if __name__` line. One feeds
`NUL-01` a blank name. One feeds `DUP-01` a repeated id.

```python
    def test_nul_fires(self):
        self.assertEqual(len(checks.CHECKS["NUL-01"](GOOD + [{"id": 5, "name": ""}])), 1)

    def test_dup_fires(self):
        repeated = GOOD + [{"id": 1, "name": "Dup", "number": 1}]
        self.assertEqual(len(checks.CHECKS["DUP-01"](repeated)), 1)
```

```
$ python deadcheck.py --registry checks:CHECKS --out report.json -- -m unittest
deadcheck: registry checks:CHECKS holds 4 check(s), 4 selected, stub value []
baseline: the test command passed with no stub
PROVEN       LEN-01  stub called 2 time(s) and the tests failed (exit 1)
PROVEN       NUL-01  stub called 2 time(s) and the tests failed (exit 1)
PROVEN       DUP-01  stub called 1 time(s) and the tests failed (exit 1)
PROVEN       RNG-01  stub called 1 time(s) and the tests failed (exit 1)
checks stubbed: 4 of 4
  PROVEN       4
  ...
VERDICT: 4 of 4 check(s) proven able to fire, 0 allowed unproven.

Check only. report.json was not written. Re-run with --apply.
```

The exit code is 0 on both hosts, and `report.json` does not exist. The same command with `--apply` printed
`wrote report.json` and the file holds one record per id with its status, hit count, exit code and reason.
Nothing from the test output goes into the report.

### Experiment 3: a typo in the check

With the fire tests in place, `NUL-01` was changed to read `r.get("nmae") == ""`. The check can no
longer fire, so the ordinary suite goes red, and the tool refuses to start:

```
$ python deadcheck.py --registry checks:CHECKS --tail 6 -- -m unittest
deadcheck: registry checks:CHECKS holds 4 check(s), 4 selected, stub value []
REFUSED   the test command fails without any stub (exit 1), so a failure under a stub would prove nothing
      |     self.assertEqual(len(checks.CHECKS["NUL-01"](GOOD + [{"id": 5, "name": ""}])), 1)
      |     ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
      | AssertionError: 0 != 1
      | ----------------------------------------------------------------------
      | Ran 6 tests in 0.001s
      | FAILED (failures=1)

VERDICT: REFUSED
```

The `~~~^^^` marker line is Python 3.13's traceback. Python 3.12 on Linux prints other traceback
lines in the tail, with the same `REFUSED` line and exit 3.

With the original fixture tests (without the fire tests) and the same typo, the run is the one in
Experiment 1: `NUL-01` is `SURVIVED`. That is the case this tool exists for.

`pytest` ran on Windows as the test command, with `--python` pointing at an environment that has
it (pytest 9.1.1): `-- -m pytest -q -p no:cacheprovider`. The result was the same as Experiment 1,
and with `--allow DUP-01` the line `allowed to stay unproven: DUP-01` was printed and the verdict
named only `NUL-01`. Without `-p no:cacheprovider`, `pytest` made a `.pytest_cache` folder in the
project folder, which is why the flag is there. `pytest` was not run on Linux, where it is not
installed.

## The self-test

`python deadcheck.py --self-test` makes a synthetic project in a temporary folder, runs the real
tool against it in real child Python processes, and removes the folder. It needs no network and no
credentials. Every behaviour this README names is one or more assertions in it. It takes under
a minute.

```
$ python deadcheck.py --self-test
deadcheck self-test: temporary files and child Python processes only, no network, no credentials
--------------------------------------------------------------------
...
PASS  stub called and tests passed is SURVIVED
...
PASS  an empty registry exits 4 and does not call itself clean  <-- pinned defect
...
PASS  a test that reads the signature of a check does not turn a dead check PROVEN  <-- pinned defect
...
PASS  a suite that fails on every run after the first does not turn a dead check PROVEN  <-- pinned defect
...
PASS  a check that returns a dict is stubbed with {}, not with a [] that crashes its caller  <-- pinned defect
...
PASS  a test that leaves a helper process running does not make the run wait for it  <-- pinned defect
...
PASS  a prefix of --apply is refused, not read as the flag  <-- pinned defect
...
PASS  a self-test with a failed assertion exits 1 and names it  <-- pinned defect
...
--------------------------------------------------------------------
213 assertions, 0 failed
```

The `...` lines stand for assertions left out of this page. Every `<-- pinned defect` line is a bug
that was found and then given an assertion, so it cannot come back. The count is the same on every
interpreter listed under Requirements. Branch coverage is 100 percent, measured on Windows with
Python 3.13:

```
$ python -m coverage run --branch deadcheck.py --self-test
$ python -m coverage report -m
Name           Stmts   Miss Branch BrPart  Cover   Missing
----------------------------------------------------------
deadcheck.py    1031      0    210      0   100%
----------------------------------------------------------
```

`coverage` is not installed on the Linux host, so branch coverage was not measured there.

The self-test can fail, and the harness is proved from outside. A separate process builds a
harness with one failed assertion and runs `main(["--self-test"])` over it. The self-test counts
the result with plain code, not with `check()`, and it requires exit status 1 for the failing
harness and 0 for the passing one. A broken `check()` or `footer()` turns the self-test red.
The one thing it cannot prove is its own last line, see Limits.

To check that the self-test fails when the tool is wrong, 36 one-line mutations of the
tool were applied to a copy, one at a time. Examples: let a failed run with no hits count as `PROVEN`, let `--out` replace a
file, switch `allow_abbrev` on, make `check()` always pass, stop repeating the check's signature
in the stub.
35 went red (3 of them by crashing the self-test, not by a failed assertion) and 1 stayed green. The one that stayed green makes `self_test()` return 0 whatever failed. The
run still prints the failed count, and only the exit status is wrong. A process cannot check its
own last line, so in CI read the footer line as well as the exit status.
The sweep script is a scratch file and is not in this repository. An earlier sweep of 38 found three
more survivors: two were equivalent mutants in code that was then deleted, and one led to the
assertion that a control run which never reached the check cannot clear a stub.

The child side of the tool runs inside the self-test process as well as in child processes, so the
lines it runs are measured. The `if __name__ == "__main__":` line is covered because coverage runs
the file as a script.

## What it will not do

- It does not show that a check is right. `PROVEN` means a test fails when the check returns
  its empty value and passes when the real check runs. That test could fail for a reason that has
  nothing to do with whether the check finds the real defect.
- It does not mutate operators or constants. Use mutmut or Cosmic Ray for that.
- It does not find checks that are not in the registry.
- It does not write a test for you.

## Limits

- **A test that runs the check in another Python process leaves it `UNCALLED`.** The stub lives in
  one process. A test that shells out to a fresh interpreter runs the real check. The self-test
  pins this.
- **A test that imports the same source under a second module name leaves it `UNCALLED`.** The
  second import makes a second function object, and the stub is only on the one the registry
  holds. The self-test pins this with a registry at `pkg.checks` and a test that imports `checks`.
- **Only plain Python functions are stubbed.** `async` functions, callable objects, builtins and
  partials are `UNSTUBBABLE` and are named in the verdict. Each reason is in the output.
- **A registry that uses one function for two ids is refused**, because a stub for one id would
  stub both, and a test for one would prove the other.
- **The stub returns a constant.** A check whose tests would still pass with that constant is
  `SURVIVED`. A caller that cannot work with the empty value, for example a test that reads the
  key `['findings']` of the empty dict, fails, and that failure is read as `PROVEN`. The
  self-test pins that case. A `contextmanager` wrapper, a number or a bool has no empty value the
  tool can pick, so it is `SUSPECT` until you pass `--stub-value`.
- **A flaky suite can still give a false `PROVEN`.** The control run catches a suite that fails
  on every run after the first. A test that fails by chance in the stubbed run and passes in the
  control run still counts. Run the suite several times before you trust a `PROVEN`.
- **A stub keeps the name, docstring, parameters and defaults of a check, not its source.**
  `inspect.getsource` on a stubbed function raises `OSError`.
- **`--timeout` ends the run, not the helpers the tests started.** The tool kills the test
  process and no longer waits for its output to close. A helper process that the tests left
  running, such as a local server, keeps running after the tool returns. The self-test pins both
  halves on Windows and on Linux.
- **Windows and Linux only.** macOS was not run.
- **The tests run more than once per check.** There is one run to list the registry, one baseline
  run, one run per check, one control run for each check that looks `PROVEN`, and one more for a
  check that needs another stub value. A suite of ten minutes and forty checks takes about seven
  hours. `--only` selects checks. `--timeout` ends a run that hangs.
- **Anything with a side effect repeats in every run.** Importing your registry is one of them: a
  registry module that opens a connection when it is imported opens it in the listing run, in
  every stubbed run and in every control run. Files that your tests write, a `pytest` cache or
  coverage data for example, are written every time.
- **`--tail` and the registry load error print your own text.** The last N lines of test output,
  and the message of an exception raised while the registry is imported, reach standard output
  unchanged. A test that prints a secret, or an import error that carries a connection string,
  shows it in a scheduler log. The self-test pins that `--tail 3` prints a secret a test prints,
  and that the default prints none.
- **Do not put secrets in the test command.** It is on the command line of the tool and of each
  child process, so other users of the machine can read it while the run lasts. This was seen with
  `Get-CimInstance Win32_Process` on Windows and `ps -eo args` on Linux. Pass secrets to your tests
  in environment variables.
- **A hostile test can forge the answer.** The tool reads the last `DEADCHECK-HITS` line of the
  output. A test that prints its own line after the real one, for example from a thread that keeps
  running, can set a wrong hit count. A test that replaces `sys.stdout` for good can only move the
  result to `UNKNOWN`.
- **A self-test cannot vouch for its own exit status.** If the last line of `self_test()` were
  changed to return 0, the run would print `1 failed` and still exit 0. Read the footer line in CI.
- **The mutation sweep of the tool is not shipped.** It is a scratch script. The assertions it
  found missing were added to the self-test.
- **No network and no credentials.** The tool imports no network library, and a self-test
  assertion checks that by reading the imports, so no stub server was needed.
- **No Esri, database, PROJ, FEMA or Census behaviour is relied on.** The tool does not touch GIS
  data. The checks in your registry may.
- **Sources checked.** Python documentation for
  [function objects and `__code__`](https://docs.python.org/3/reference/datamodel.html),
  [`inspect.signature`](https://docs.python.org/3/library/inspect.html),
  [`importlib.util.spec_from_file_location`](https://docs.python.org/3/library/importlib.html),
  [`runpy`](https://docs.python.org/3/library/runpy.html),
  [`subprocess.run` and its timeout](https://docs.python.org/3/library/subprocess.html),
  [`tempfile.TemporaryFile`](https://docs.python.org/3/library/tempfile.html),
  [`argparse` and `allow_abbrev`](https://docs.python.org/3/library/argparse.html),
  [`ast.literal_eval`](https://docs.python.org/3/library/ast.html),
  [`math.isfinite`](https://docs.python.org/3/library/math.html) and the
  [`PYTHONDONTWRITEBYTECODE`, `PYTHONIOENCODING` and `PYTHONSAFEPATH` variables](https://docs.python.org/3/using/cmdline.html).
  The POSIX [`_exit`](https://pubs.opengroup.org/onlinepubs/9799919799/functions/_exit.html)
  page says the parent sees only the low-order 8 bits of the status, which is why `sys.exit(256)`
  reads as 0 on Linux. Three behaviours are not in those pages and were found by running them on
  Python 3.12 and 3.13. Giving a function a code object with a different number of free
  variables raises `ValueError`, so the stub is built with the same number as the check it
  replaces. Giving a generator function the code of a plain function emits a
  `DeprecationWarning`, so a generator check gets a generator stub. On Windows, `subprocess.run`
  with a pipe waits for every process that holds the pipe open after the timeout, which is why
  the output goes to a file. All three are pinned in the self-test.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [ng911check](https://github.com/uhsear/ng911check) - a registry of documented checks run from a CSV, the kind of registry this tool proves
- [alwayszero](https://github.com/uhsear/alwayszero) - names unattended scripts that cannot report failure, the same silent-green problem in scheduled jobs
- [failstorm](https://github.com/uhsear/failstorm) - a majority-flip defect pinned in tests, with a hold rule for stdlib cron scripts
