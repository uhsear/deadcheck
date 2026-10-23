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
in a green suite for years.

One Python file, standard library only, Python 3.9 or later. It runs the same on Windows and on
Linux. It writes nothing unless you pass `--apply`.

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
README.

This tool asks one narrower question and keeps the answer per check id. A mutation tester mutates
operators and reports the mutants that survive. It does not know that your code has a registry. Here the
unit is the registered check, and the answer is one of seven named statuses for each id, with an
exit code a scheduled job can read. One stub per check also costs one test run per check, not one
per mutant.

A search of GitHub on the day this was written (`gh search repos deadcheck`) found other
repositories with this name. They do other work: a dead man's switch service, a dead-code
finder for JavaScript, a dead link finder for documentation, and a tool that scores a grading
rubric. A search for `mutation testing` in Python returned the four tools above among its first results. This is an
absence of results in one search, not a claim that nothing else exists.

## Requirements

- Python 3.9 or later. No other package. `--self-test` ran green on 3.9.25, 3.10, 3.12 and 3.13
  on Windows, and on 3.12 on Linux, with the same assertion count on every one.
- A test command that Python can run: `-m module args` or `script.py args`. `unittest` and a plain
  script are exercised by the self-test. `pytest` was run by hand, see Experiment 3.
- A registry: a `dict` of id to function, a list of functions (the function name is the id), or
  a list of `(id, function)` pairs.

## Usage

```
python deadcheck.py --registry MODULE:NAME [options] -- TEST COMMAND
python deadcheck.py --self-test
```

| Option | What it does |
|---|---|
| `--registry MODULE:NAME` | Where the checks are. `NAME` may be dotted. Required. |
| `--cwd DIR` | Folder to run in and to import from. Default `.`. |
| `--python EXE` | The Python that runs the tests. Default: the one running this tool. |
| `--stub-value LITERAL` | What a stub returns, as a Python literal. Default `[]`. Code is refused. |
| `--only ID` | Stub only this check. Repeat to name more. |
| `--allow ID` | A check that may stay unproven without failing the run. Repeat to name more. |
| `--timeout SEC` | Seconds one test run may take. Default 600. |
| `--tail N` | Print the last N lines of the test output under a SUSPECT or UNKNOWN check. |
| `--out FILE` | The JSON report. A new file only. Needs `--apply` to be written. |
| `--apply` | Write `--out`. Nothing else in the tool writes anything. |
| `--self-test` | Run the offline assertions and exit. |

The parser has `allow_abbrev=False`. A prefix such as `--ap` is an error, not `--apply`. The
self-test pins five prefixes, and the `--apply` one is marked as a pinned defect.

### The seven statuses

| Status | The stub was called | The tests | Meaning |
|---|---|---|---|
| `PROVEN` | yes | failed | A test needs this check to return something. |
| `SURVIVED` | yes | passed | A test runs the check and asserts nothing about what it found. |
| `UNCALLED` | no | passed | No test runs the check. |
| `SUSPECT` | no | failed | The failure is not this check's. It proves nothing. |
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

## How it works

1. A child Python process imports the registry and lists the ids. The parent refuses a registry
   with a repeated id, or with two ids on one function.
2. A baseline run of your tests, with no stub. If it fails, the tool stops with exit 3. A failure
   under a stub would prove nothing.
3. For each id, a fresh child process imports the registry, replaces the code object of that one
   function with a stub, and runs your tests by `runpy`. The stub counts its calls and returns the
   literal. The child prints the count on a marker line.
4. The parent reads the exit code and the count, and picks the status.

Replacing `__code__` means a test that imports the function directly is stubbed as well as a test
that goes through the registry. The tool uses no fork, no signal and no file, which is why it runs
on Windows. The decision core (`classify`, `verdict`, `explain`, `shared_ids` and the parsers) takes
values and returns values. The processes, the files and the printing are in separate functions.

## Experiments

The numbers below are from real runs on a synthetic project: four checks in a registry built with
a decorator, so each registered function is a `functools.wraps` wrapper.

### Experiment 1: a suite with clean-sample tests

The run in the opening is this one. `LEN-01` and `RNG-01` have a test that feeds them a bad row.
`NUL-01` has a test that feeds it good rows. `DUP-01` has none. Same result on Windows (Python 3.13)
and on Linux (Python 3.12): `2 of 4 ... not proven: NUL-01, DUP-01`.

### Experiment 2: add the missing tests

After a test that feeds `NUL-01` a blank name, and one that feeds `DUP-01` a repeated id:

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

With the fire test in place, `NUL-01` was changed to read `r.get("nmae")`. The check can no longer
fire, so the ordinary suite goes red, and the tool refuses to start:

```
$ python deadcheck.py --registry checks:CHECKS --tail 6 -- -m unittest
deadcheck: registry checks:CHECKS holds 4 check(s), 4 selected, stub value []
REFUSED   the test command fails without any stub (exit 1), so a failure under a stub would prove nothing
      | - []
      | + [5]
      | ?  +
      | ----------------------------------------------------------------------
      | Ran 6 tests in 0.001s
      | FAILED (failures=1)

VERDICT: REFUSED
```

With only the clean-sample test, the same typo is `SURVIVED`, which is Experiment 1. That is the
case this tool exists for.

`pytest` ran on Windows as the test command, with `--python` pointing at an environment that has
it: `-- -m pytest -q -p no:cacheprovider`. The result was the same as Experiment 1, and with
`--allow DUP-01` the line `allowed to stay unproven: DUP-01` was printed and the verdict named only
`NUL-01`. `pytest` was not run on Linux, where it is not installed.

## The self-test

`python deadcheck.py --self-test` makes a synthetic project in a temporary folder, runs the real
tool against it in real child Python processes, and removes the folder. It needs no network and no
credentials.

```
$ python deadcheck.py --self-test
deadcheck self-test: temporary files and child Python processes only, no network, no credentials
--------------------------------------------------------------------
...
PASS  stub called and tests passed is SURVIVED
PASS  stub never called and tests passed is UNCALLED
...
PASS  an empty registry exits 4 and does not call itself clean  <-- pinned defect
...
PASS  a generator function is stubbed with a generator that yields nothing and Python raises no deprecation warning  <-- pinned defect
...
PASS  a check only a clean-sample test runs is SURVIVED, the case a clean-sample test leaves open
PASS  a check no test runs is UNCALLED
...
PASS  --out will not replace a file that is there, on a dry run too
...
PASS  a report file that appears while the tests run is not replaced, exit 2
...
PASS  a prefix of --apply is refused, not read as the flag  <-- pinned defect
...
PASS  a refused prefix of --apply wrote nothing
...
PASS  two ids on one function are REFUSED, a stub for one would prove the other  <-- pinned defect
PASS  an empty registry exits 4, because a run that checked nothing is not clean  <-- pinned defect
...
PASS  a test that runs the check in a new Python process of its own leaves it UNCALLED
...
PASS  check() and raises() really do record a failure  <-- pinned defect
...
PASS  an environment secret reaches neither stdout, stderr nor the report
...
--------------------------------------------------------------------
149 assertions, 0 failed
```

The `...` lines stand for assertions left out of this page. Every `<-- pinned defect` line is a bug
that was found and then given an assertion, so it cannot come back. The count is the same on every
interpreter listed under Requirements. Branch coverage is 100 percent, on Windows with Python 3.13
and on Linux with Python 3.12:

```
$ python -m coverage run --branch deadcheck.py --self-test
$ python -m coverage report -m
Name           Stmts   Miss Branch BrPart  Cover   Missing
----------------------------------------------------------
deadcheck.py     790      0    162      0   100%
----------------------------------------------------------
```

To check that the self-test can fail, 33 one-line mutations of the tool were applied to a copy,
one at a time. Examples: let a failed run with no hits count as `PROVEN`, let `--out` replace a
file, switch `allow_abbrev` on, stop refusing a shared function. The self-test went red for all 33.
The sweep script is a scratch file and is not in this repository.

The child side of the tool runs inside the self-test process as well as in child processes, so the
lines it runs are measured. The `if __name__ == "__main__":` line is covered because coverage runs
the file as a script.

## What it will not do

- It does not show that a check is right. `PROVEN` means a test fails when the check returns
  nothing. That test could fail for a reason that has nothing to do with whether the check finds
  the real defect.
- It does not mutate operators or constants. Use mutmut or Cosmic Ray for that.
- It does not find checks that are not in the registry.
- It does not write a test for you.

## Limits

- **A test that runs the check in another Python process leaves it `UNCALLED`.** The stub lives in
  one process. A test that shells out to a fresh interpreter runs the real check. The self-test
  pins this.
- **Only plain Python functions are stubbed.** `async` functions, callable objects, builtins and
  partials are `UNSTUBBABLE` and are named in the verdict. Each reason is in the output.
- **A registry that uses one function for two ids is refused**, because a stub for one id would
  stub both, and a test for one would prove the other.
- **The stub returns a constant.** A check whose tests would still pass with that constant is
  `SURVIVED`. Use `--stub-value` when `[]` is the wrong empty value for your registry.
- **A timeout stops the child process, not the processes the tests started.** This was not tested
  with a test that starts its own long-running grandchild.
- **Windows and Linux only.** macOS was not run.
- **The tests run once per check.** A suite of ten minutes and forty checks takes about seven
  hours. `--only` selects checks, and `--timeout` stops a run that hangs.
- **No network and no credentials.** The tool imports no network library, and a self-test
  assertion checks that by reading the imports, so no stub server was needed. A canary
  environment variable is set in one run and must not appear in the output or in the report.
- **No Esri, database, PROJ, FEMA or Census behaviour is relied on.** The tool does not touch GIS
  data. The checks in your registry may.
- **Sources checked.** Python documentation for
  [function objects and `__code__`](https://docs.python.org/3/reference/datamodel.html),
  [`runpy`](https://docs.python.org/3/library/runpy.html),
  [`subprocess.run` and its timeout](https://docs.python.org/3/library/subprocess.html),
  [`argparse` and `allow_abbrev`](https://docs.python.org/3/library/argparse.html),
  [`ast.literal_eval`](https://docs.python.org/3/library/ast.html) and the
  [`PYTHONDONTWRITEBYTECODE` and `PYTHONSAFEPATH` variables](https://docs.python.org/3/using/cmdline.html).
  Two behaviours are not in those pages and were found by running them on Python 3.12 and 3.13.
  Giving a function a code object with a different number of free variables raises `ValueError`,
  so the stub is built with the same number as the check it replaces. Giving a generator function
  the code of a plain function emits a `DeprecationWarning`, so a generator check gets a
  generator stub. Both are pinned in the self-test.

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
