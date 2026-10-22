#!/usr/bin/env python
"""Prove that each registered check can fire, by stubbing it out and watching the tests.

A check registry is a dict or list of functions, each one hunting for one kind
of defect and returning the findings. The usual guard is a test suite, and the
usual suite has a clean-sample test for every check: feed it good data, expect
no findings. That test still passes when the check body is replaced by
"return []". A check that can never fire, because of a typo in a field name or
a branch nothing reaches, passes it too. Nothing is red, and the check is dead.

This stubs one registered check at a time so that it returns nothing, runs your
test command in a child process, and reports what happened:

    PROVEN       the stub was called and the tests failed: a test needs it to fire
    SURVIVED     the stub was called and the tests still passed: a test runs the
                 check and asserts nothing about what it found
    UNCALLED     the tests passed and never called the stub: no test runs it
    SUSPECT      the tests failed but never called the stub: not this check's failure
    TIMEOUT      the tests ran past --timeout
    UNKNOWN      the tests ended before they could report whether the stub was called
    UNSTUBBABLE  the check is not a plain Python function, so it was not stubbed

The stub replaces the function's code object, so a test that imports the
function directly is stubbed as well as a test that goes through the registry.
No fork, no signal and no file is used, so it runs the same on Windows as on
Linux. The tests run in a child Python process by runpy, so any
"-m module args" or "script.py args" that Python can run will do.

    python deadcheck.py --self-test
    python deadcheck.py --registry checks:CHECKS -- -m unittest discover -s tests
    python deadcheck.py --registry pkg.mod:CHECKS --cwd C:/work/proj -- tests/run.py
    python deadcheck.py --registry checks:CHECKS --stub-value None -- -m unittest
    python deadcheck.py --registry checks:CHECKS --only RULE-01 -- -m unittest
    python deadcheck.py --registry checks:CHECKS --allow RULE-09 -- -m unittest
    python deadcheck.py --registry checks:CHECKS --tail 5 -- -m unittest
    python deadcheck.py --registry checks:CHECKS --out report.json --apply -- -m unittest

The obvious tools mutate a program's operators and report the mutants your tests
miss. mutmut and Cosmic Ray do that, and do it far more thoroughly. This asks the
one narrower question the registry asks of itself, once per check, and keeps the
answer per check id. Nothing is written without --apply.

Exit codes: 0 every selected check was proven or is allowed, 1 at least one was
not proven, 2 a process could not be started or a file could not be written, 3
refused (the baseline test run failed, the registry could not be loaded, or two
checks share an id), 4 nothing was checked, 64 usage error, 70 an unexpected
crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import ast
import contextlib
import importlib
import importlib.util
import inspect
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import traceback
import types
import warnings

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Seconds one test run may take, the baseline included, before it is cut off.
DEFAULT_TIMEOUT = 600

# What a stubbed check returns. A Python literal, so it cannot run code.
DEFAULT_STUB_VALUE = "[]"

PROVEN = "PROVEN"
SURVIVED = "SURVIVED"
UNCALLED = "UNCALLED"
SUSPECT = "SUSPECT"
TIMEOUT = "TIMEOUT"
UNKNOWN = "UNKNOWN"
UNSTUBBABLE = "UNSTUBBABLE"
STATUSES = (PROVEN, SURVIVED, UNCALLED, SUSPECT, TIMEOUT, UNKNOWN, UNSTUBBABLE)

EXIT_OK = 0
EXIT_NOT_PROVEN = 1
EXIT_IO = 2
EXIT_REFUSED = 3
EXIT_NOTHING = 4
EXIT_USAGE = 64
EXIT_CRASH = 70

# The child prints its answers as marker lines. The tests print what they like
# around them, so the parent looks for these and nothing else.
_PROBE = "_deadcheck_probe"
_ENTRY = "import sys; sys.path.insert(0, sys.argv.pop(1)); import %s as m; " \
         "sys.path.pop(0); sys.modules.pop(%r); sys.exit(m.child_main(sys.argv[1:]))"


class Unstubbable(Exception):
    """The check cannot be replaced by a stub. The message says why."""


# =============================================================================
# The decision core. Pure: no process, no file, no clock.
# =============================================================================

def parse_stub_value(text):
    """The Python literal a stubbed check returns. Raises ValueError for anything else."""
    try:
        value = ast.literal_eval(text)
        # The stub is built from repr(value), so a value that does not read back
        # (1e999 prints as inf) would fail inside the tests, for the wrong reason.
        ast.literal_eval(repr(value))
        return value
    except (ValueError, SyntaxError):
        raise ValueError("--stub-value must be one Python literal such as [], None or {}; "
                         "got %r" % text)


def stub_source(free_count, check_id, value, generator=False):
    """Source of a function that records one call and returns value.

    A generator check gets a generator stub that yields nothing, because Python
    deprecates giving a generator function the code of a plain function. Its
    value is ignored.

    The function needs as many free variables as the check it replaces, because
    Python refuses to give a function a code object with a different count. The
    names sit in dead code so the stub never reads a cell it does not own.
    """
    lines = ["def _outer():"]
    names = ["_free%d" % n for n in range(free_count)]
    if names:
        lines.append("    %s = None" % " = ".join(names))
    lines.append("    def _stub(*args, **kwargs):")
    if names:
        lines.append("        if 0:")
        lines.extend("            %s" % name for name in names)
    lines.append("        probe = __import__('sys').modules[%r]" % _PROBE)
    lines.append("        probe.hits[%r] = probe.hits.get(%r, 0) + 1" % (check_id, check_id))
    if generator:
        lines.append("        if 0:")
        lines.append("            yield")
    else:
        lines.append("        return %s" % repr(value))
    lines.append("    return _stub")
    return "\n".join(lines) + "\n"


def build_stub_code(free_count, check_id, value, generator=False):
    """The code object for a stub with free_count free variables."""
    namespace = {}
    exec(compile(stub_source(free_count, check_id, value, generator), "<deadcheck-stub>",
                 "exec"),
         namespace)
    return namespace["_outer"]().__code__


def find_marker(text, name):
    """The text after the last 'DEADCHECK-<name> ' line, or None when there is none."""
    found = re.findall(r"^DEADCHECK-%s (.*?)\r?$" % name, text, re.M)
    return found[-1] if found else None


def parse_hits(text):
    """How many times the stub ran, as the child reported it, or None."""
    value = find_marker(text, "HITS")
    return int(value) if value is not None and value.isdigit() else None


def parse_ids(text):
    """The registered check ids the child listed, or None when it listed none."""
    value = find_marker(text, "IDS")
    return json.loads(value) if value is not None else None


def duplicate_ids(ids):
    """Every id that appears more than once, in first-seen order."""
    seen, repeated = set(), []
    for ident in ids:
        if ident in seen and ident not in repeated:
            repeated.append(ident)
        seen.add(ident)
    return repeated


def unknown_ids(wanted, ids):
    """The ids in wanted that the registry does not hold."""
    return [ident for ident in wanted if ident not in ids]


def check_command(command):
    """Why the test command cannot be run, or None."""
    if not command:
        return "name the test command after --, for example: -- -m unittest discover"
    if command[0] == "-m" and len(command) < 2:
        return "-m needs a module name"
    return None


def classify(returncode, hits, timed_out, unstubbable):
    """The status of one stubbed run. The order of the tests matters.

    A run that failed without calling the stub proves nothing about the check,
    and neither does a run that could not say how often it called it.
    """
    if unstubbable:
        return UNSTUBBABLE
    if timed_out:
        return TIMEOUT
    if hits is None:
        return UNKNOWN
    if returncode == 0:
        return SURVIVED if hits > 0 else UNCALLED
    return PROVEN if hits > 0 else SUSPECT


def explain(status, returncode, hits, timeout, reason):
    """One line saying what the status means for this run."""
    if status == PROVEN:
        return ("stub called %d time(s) and the tests failed (exit %s)"
                % (hits, returncode))
    if status == SURVIVED:
        return ("stub called %d time(s) and the tests still passed: a test runs "
                "this check and asserts nothing about what it found" % hits)
    if status == UNCALLED:
        return "the tests passed and never called the stub: no test runs this check"
    if status == SUSPECT:
        return ("the tests failed (exit %s) without calling the stub, so the "
                "failure is not this check's and proves nothing" % returncode)
    if status == TIMEOUT:
        return "the tests ran past %s seconds" % timeout
    if status == UNKNOWN:
        return ("the tests ended before they reported whether the stub was "
                "called (exit %s)" % returncode)
    return reason


def summarize(results):
    """Count of results per status, every status present."""
    counts = dict((status, 0) for status in STATUSES)
    for result in results:
        counts[result["status"]] += 1
    return counts


def verdict(results, allow):
    """(exit code, lines) for a list of results and the ids allowed to stay unproven."""
    total = len(results)
    if not total:
        return EXIT_NOTHING, [
            "VERDICT: NOTHING CHECKED. The registry holds no check, so no check "
            "was shown able to fire. This is not a clean result."]
    unproven = [r["id"] for r in results if r["status"] != PROVEN and r["id"] not in allow]
    waived = [r["id"] for r in results if r["status"] != PROVEN and r["id"] in allow]
    stale = [r["id"] for r in results if r["status"] == PROVEN and r["id"] in allow]
    proven = sum(1 for r in results if r["status"] == PROVEN)
    lines = []
    if waived:
        lines.append("allowed to stay unproven: %s" % ", ".join(waived))
    if stale:
        lines.append("allowed but proven, drop them from --allow: %s" % ", ".join(stale))
    if unproven:
        lines.append("VERDICT: %d of %d check(s) proven able to fire, %d not proven: %s. "
                     "A check no test needs to fire may be unable to fire."
                     % (proven, total, len(unproven), ", ".join(unproven)))
        return EXIT_NOT_PROVEN, lines
    lines.append("VERDICT: %d of %d check(s) proven able to fire, %d allowed unproven."
                 % (proven, total, len(waived)))
    return EXIT_OK, lines


def render_report(results, counts):
    """The JSON text --apply writes. Ids and statuses only, never test output."""
    return json.dumps({"counts": counts, "checks": results}, indent=2, sort_keys=True) + "\n"


# =============================================================================
# The child side. This runs inside the process that runs your tests.
# =============================================================================

def entries_of(registry):
    """(id, function) pairs from a dict, a list of functions or a list of (id, function)."""
    items = list(registry.items()) if isinstance(registry, dict) else list(registry)
    entries = []
    for index, item in enumerate(items):
        if isinstance(item, tuple):
            ident, func = item if len(item) == 2 else (None, None)
        else:
            ident, func = getattr(item, "__name__", None), item
        if not (isinstance(ident, str) and callable(func)):
            raise ValueError("registry item %d is not a check: it needs a str id and "
                             "a callable" % index)
        entries.append((ident, func))
    return entries


def resolve_registry(spec):
    """Import module:NAME (NAME may be dotted) and read the checks out of it."""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError("--registry must look like module:NAME, got %r" % spec)
    obj = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return entries_of(obj)


def apply_stub(func, check_id, value):
    """Replace the code of func so it records a call and returns value.

    shortcut: a callable object, a functools.partial or a builtin has no code to
    replace, so it is refused here instead of being stubbed some other way.
    """
    target = getattr(func, "__func__", func)
    if not isinstance(target, types.FunctionType):
        raise Unstubbable("%s is not a plain Python function, so it cannot be "
                          "stubbed" % type(func).__name__)
    if inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target):
        raise Unstubbable("an async function awaits its result, so a stub that "
                          "returns a plain value would fail for the wrong reason")
    target.__code__ = build_stub_code(len(target.__code__.co_freevars), check_id, value,
                                      inspect.isgeneratorfunction(target))


def _marker(name, text):
    """Print one marker line, ASCII only, on a line of its own."""
    safe = text.encode("ascii", "replace").decode("ascii")
    print("\nDEADCHECK-%s %s" % (name, safe))
    sys.stdout.flush()


def run_target(argv):
    """Run '-m module args' or 'script args' in this process; return its exit code."""
    try:
        if argv[0] == "-m":
            sys.argv = [argv[1]] + list(argv[2:])
            runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
        else:
            sys.argv = list(argv)
            runpy.run_path(argv[0], run_name="__main__")
        return 0
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 1


def child_main(argv):
    """The entry point of the child process. argv[0] is the JSON job."""
    job = json.loads(argv[0])
    sys.path.insert(0, job["cwd"])
    if job["argv"][0] != "-m":
        sys.path.insert(0, os.path.dirname(os.path.abspath(job["argv"][0])))
    probe = types.ModuleType(_PROBE)
    probe.hits = {}
    sys.modules[_PROBE] = probe
    mode, check_id = job["mode"], job.get("id")
    if mode != "baseline":
        try:
            entries = resolve_registry(job["registry"])
        except (Exception, SystemExit) as exc:
            _marker("ERROR", "%s: %s" % (type(exc).__name__, exc))
            return 1
        if mode == "list":
            _marker("IDS", json.dumps([ident for ident, _func in entries]))
            return 0
        try:
            apply_stub(dict(entries)[check_id], check_id, parse_stub_value(job["value"]))
        except Unstubbable as exc:
            _marker("UNSTUBBABLE", str(exc))
            return 1
    code = run_target(job["argv"])
    _marker("HITS", str(probe.hits.get(check_id, 0)))
    return code


# =============================================================================
# The parent side: start the children, print, write the report.
# =============================================================================

def run_child(python, job, cwd, timeout):
    """Run one job in a fresh Python. Returns (exit code, text, timed out)."""
    here = os.path.abspath(__file__)
    stem = os.path.splitext(os.path.basename(here))[0]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [python, "-c", _ENTRY % (stem, stem), os.path.dirname(here), json.dumps(job)]
    try:
        done = subprocess.run(command, cwd=cwd, env=env, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        return None, "", True
    return done.returncode, done.stdout.decode("utf-8", "replace"), False


def tail_of(text, count):
    """The last count non-blank lines, each cut to 160 characters."""
    lines = [line.rstrip()[:160] for line in text.splitlines()
             if line.strip() and not line.startswith("DEADCHECK-")]
    return lines[len(lines) - count:] if count else []


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode.

    A piped stdout on Windows is cp1252. A check id outside it would raise
    UnicodeEncodeError and the verdict would never print. It prints as ? instead.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="deadcheck.py",
        allow_abbrev=False,
        description="Prove that each registered check can fire, by stubbing it "
                    "out and running your tests.",
        epilog="Put the test command after --, for example: "
               "deadcheck.py --registry checks:CHECKS -- -m unittest discover. "
               "Nothing is written without --apply.")
    ap.add_argument("command", nargs="*",
                    help="the test command, as '-m module args' or 'script args', after --")
    ap.add_argument("--registry", metavar="MODULE:NAME",
                    help="where the checks are: a dict of id to function, or a list of "
                         "functions or (id, function) pairs. NAME may be dotted.")
    ap.add_argument("--cwd", default=".",
                    help="directory to run in and to import from (default .)")
    ap.add_argument("--python", default=sys.executable,
                    help="the Python that runs the tests (default: this one)")
    ap.add_argument("--stub-value", dest="stub_value", default=DEFAULT_STUB_VALUE,
                    help="what a stubbed check returns, as a Python literal "
                         "(default %s)" % DEFAULT_STUB_VALUE)
    ap.add_argument("--only", action="append", default=[], metavar="ID",
                    help="stub only this check. Repeat to name more.")
    ap.add_argument("--allow", action="append", default=[], metavar="ID",
                    help="a check that may stay unproven without failing the run. "
                         "Repeat to name more.")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="seconds one test run may take (default %d)" % DEFAULT_TIMEOUT)
    ap.add_argument("--tail", type=int, default=0,
                    help="print this many last lines of the test output under a "
                         "SUSPECT or UNKNOWN check (default 0, none)")
    ap.add_argument("--out", help="write the report as JSON to this new file. Needs --apply.")
    ap.add_argument("--apply", action="store_true",
                    help="actually write --out. Without it nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a check was not proven'."""
    _tolerate_odd_characters()
    try:
        return _run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about your checks." % (type(exc).__name__, exc), file=sys.stderr)
        traceback.print_exc()
        return EXIT_CRASH


def _usage(message):
    print("error: %s" % message, file=sys.stderr)
    return EXIT_USAGE


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 for a flag it refuses, and 2 here means an I/O failure.
        return EXIT_USAGE if exc.code == 2 else exc.code
    if args.self_test:
        return self_test()

    if not args.registry:
        return _usage("--registry is required. Use --self-test to verify the tool "
                      "without a project.")
    problem = check_command(args.command)
    if problem:
        return _usage(problem)
    if args.timeout <= 0:
        return _usage("--timeout must be above zero.")
    if args.tail < 0:
        return _usage("--tail cannot be negative.")
    if args.apply and not args.out:
        return _usage("--apply needs --out.")
    if args.out and os.path.lexists(args.out):
        # Checked on a dry run too, so a path that would replace a test file is
        # named before --apply is added, not after.
        return _usage("--out %s already exists. Name a new file." % args.out)
    if not os.path.isdir(args.cwd):
        return _usage("--cwd %s is not a directory." % args.cwd)
    try:
        value = parse_stub_value(args.stub_value)
    except ValueError as exc:
        return _usage(str(exc))

    cwd = os.path.abspath(args.cwd)
    base = {"cwd": cwd, "argv": args.command, "registry": args.registry,
            "value": args.stub_value}
    try:
        code, text, timed_out = run_child(args.python, dict(base, mode="list"), cwd,
                                          args.timeout)
        ids = None if timed_out else parse_ids(text)
        if ids is None:
            why = find_marker(text, "ERROR") or (
                "the registry listing ran past %s seconds" % args.timeout if timed_out
                else "the registry listing ended with exit %s and no answer" % code)
            print("REFUSED   the registry could not be loaded: %s" % why)
            print("\nVERDICT: REFUSED")
            return EXIT_REFUSED
        repeated = duplicate_ids(ids)
        if repeated:
            print("REFUSED   the registry is not unique, so nothing was stubbed")
            print("      id(s) on more than one entry: %s" % ", ".join(repeated))
            print("      A stub for an id two entries share cannot say which entry a test needs.")
            print("\nVERDICT: REFUSED")
            return EXIT_REFUSED
        missing = unknown_ids(args.only + args.allow, ids)
        if missing:
            return _usage("not registered: %s" % ", ".join(missing))
        chosen = [ident for ident in ids if not args.only or ident in args.only]
        if not chosen:
            # Decided before the baseline: a test runner with nothing to run may exit non-zero.
            print("deadcheck: registry %s holds no check" % args.registry)
            print("")
            exit_code, lines = verdict([], args.allow)
            print(lines[0])
            return exit_code

        print("deadcheck: registry %s holds %d check(s), %d selected, stub value %s"
              % (args.registry, len(ids), len(chosen), repr(value)))
        code, text, timed_out = run_child(args.python, dict(base, mode="baseline"), cwd,
                                          args.timeout)
        if timed_out or code != 0:
            print("REFUSED   the test command fails without any stub (%s), so a failure "
                  "under a stub would prove nothing"
                  % ("past %s seconds" % args.timeout if timed_out else "exit %s" % code))
            for line in tail_of(text, args.tail):
                print("      | %s" % line)
            print("\nVERDICT: REFUSED")
            return EXIT_REFUSED
        print("baseline: the test command passed with no stub")

        results = []
        for ident in chosen:
            code, text, timed_out = run_child(args.python, dict(base, mode="run", id=ident),
                                              cwd, args.timeout)
            reason = find_marker(text, "UNSTUBBABLE")
            hits = parse_hits(text)
            status = classify(code, hits, timed_out, reason is not None)
            why = explain(status, code, hits, args.timeout, reason)
            results.append({"id": ident, "status": status, "hits": hits, "exit": code,
                            "why": why})
            print("%-12s %s  %s" % (status, ident, why))
            if status in (SUSPECT, UNKNOWN):
                for line in tail_of(text, args.tail):
                    print("      | %s" % line)
    except OSError as exc:
        print("error: could not start %s: %s" % (args.python, exc), file=sys.stderr)
        return EXIT_IO

    counts = summarize(results)
    print("checks stubbed: %d of %d" % (len(results), len(ids)))
    for status in STATUSES:
        print("  %-12s %d" % (status, counts[status]))
    print("")
    exit_code, lines = verdict(results, args.allow)
    for line in lines:
        print(line)
    if args.out:
        if not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply." % args.out)
        else:
            try:
                with open(args.out, "w") as handle:
                    handle.write(render_report(results, counts))
            except (IOError, OSError) as exc:
                print("error: %s" % exc, file=sys.stderr)
                return EXIT_IO
            print("\nwrote %s" % args.out)
    return exit_code


# =============================================================================
# The self-test.
# =============================================================================

class _Harness(object):
    """The check()/raises() recorder, a class so the self-test can test it."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label):
        try:
            fn()
        except ValueError:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def footer(self):
        """Print the closing line and return the process exit status."""
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


# The synthetic project the self-test stubs. Six checks, one per behaviour.
_FIXTURE_CHECKS = '''\
CHECKS = {}


def rule_one(rows):
    return [r for r in rows if r < 0]


def rule_two(rows):
    return [r for r in set(rows) if rows.count(r) > 1]


def rule_three(rows):
    return [r for r in rows if r > 1000]


def _wrap(fn):
    def wrapper(rows):
        return fn(rows)
    return wrapper


def rule_four(rows):
    return [r for r in rows if r == 0]


async def rule_five(rows):
    return []


class RuleSix(object):
    def __call__(self, rows):
        return []


CHECKS["RULE-01"] = rule_one
CHECKS["RULE-02"] = rule_two
CHECKS["RULE-03"] = rule_three
CHECKS["RULE-04"] = _wrap(rule_four)
CHECKS["RULE-05"] = rule_five
CHECKS["RULE-06"] = RuleSix()
'''

_FIXTURE_TESTS = '''\
import unittest

import checks
from checks import rule_one


class Tests(unittest.TestCase):
    def test_rule_one_fires(self):
        self.assertEqual(rule_one([3, -2]), [-2])

    def test_rule_two_clean_sample_only(self):
        self.assertEqual(checks.CHECKS["RULE-02"]([1, 2, 3]), [])

    def test_rule_four_fires_through_a_wrapper(self):
        self.assertEqual(checks.CHECKS["RULE-04"]([0, 5]), [0])


if __name__ == "__main__":
    unittest.main()
'''


# Functions the self-test stubs in its own process. They are compiled from a
# string so their bodies are not counted as code the tool failed to run.
_STUB_TARGETS = '''\
def f_one(): pass
def f_two(): pass
def plain(rows): return [r for r in rows if r < 0]
class Holder(object):
    def method(self, rows): return [r for r in rows if r < 0]
def with_default(rows, flag=True, *, extra=3): return [flag, extra]
def outer():
    captured = [-1]
    def inner(rows): return captured + rows
    return inner
def generator(rows):
    for r in rows:
        yield r
async def coroutine(rows): return []
async def agen(rows):
    yield 1
'''


def _self_test(tmp):
    harness = _Harness()
    check = harness.check
    raises = harness.raises

    def write(folder, name, text):
        path = os.path.join(folder, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def make(name, files):
        """A project folder under tmp holding the named files."""
        folder = os.path.join(tmp, name)
        os.mkdir(folder)
        for fname, text in files.items():
            write(folder, fname, text)
        return folder

    def cli(argv):
        """main() with its output captured, so the self-test stays readable."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def environ(**changes):
        """Set (or, for None, remove) environment variables, then put them back."""
        saved = dict((name, os.environ.get(name)) for name in changes)
        try:
            for name, value in changes.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            yield
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def in_process(job):
        """child_main in this process, then take its modules back out."""
        before = set(sys.modules)
        saved_path, saved_argv = list(sys.path), list(sys.argv)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = child_main([json.dumps(job)])
        finally:
            sys.path[:] = saved_path
            sys.argv[:] = saved_argv
            for name in set(sys.modules) - before:
                sys.modules.pop(name, None)
            sys.modules.pop(_PROBE, None)
        return code, out.getvalue()

    # ---- the stub: what it is made of
    check(parse_stub_value("[]") == [] and parse_stub_value("None") is None
          and parse_stub_value("{'a': 1}") == {"a": 1},
          "the stub value is read as a Python literal")
    raises(lambda: parse_stub_value("__import__('os')"),
           "a stub value that is code and not a literal is refused")
    raises(lambda: parse_stub_value("[1,"), "a stub value that is not valid syntax is refused")
    raises(lambda: parse_stub_value("1e999"),
           "a stub value that prints as inf and would not read back is refused  <-- pinned defect")
    for count in (0, 1, 3):
        code = build_stub_code(count, "X-1", [])
        check(len(code.co_freevars) == count,
              "a stub built for %d free variable(s) has exactly that many" % count)
    source = stub_source(0, "X-1", None)
    check("if 0:" not in source and "return None" in source,
          "a stub with no free variables has no dead code and returns the literal")
    check("if 0:" in stub_source(2, "X-1", []),
          "free variables sit in dead code so the stub never reads a cell")
    check(stub_source(0, "it's", []).count("hits") == 2,
          "an id with a quote in it is written as a safe literal")

    # ---- markers
    sample = "noise\nDEADCHECK-HITS 2\nmore\r\nDEADCHECK-HITS 5\r\n"
    check(find_marker(sample, "HITS") == "5", "the last marker line wins")
    check(find_marker("no marker here", "HITS") is None, "no marker is None")
    check(parse_hits(sample) == 5, "hits are read as an integer")
    check(parse_hits("DEADCHECK-HITS lots") is None and parse_hits("") is None,
          "hits that are not a number, or absent, are unknown rather than zero"
          "  <-- pinned defect")
    check(parse_ids('DEADCHECK-IDS ["a", "b"]') == ["a", "b"] and parse_ids("x") is None,
          "ids are read from the marker and absent when there is none")
    check(find_marker("DEADCHECK-HITS 3 \n", "HITS") == "3 ",
          "a marker keeps the text after its name as written")

    # ---- the registry
    targets = {}
    exec(compile(_STUB_TARGETS, "<fixture targets>", "exec"), targets)
    f_one, f_two = targets["f_one"], targets["f_two"]
    check([i for i, _f in entries_of({"A": f_one, "B": f_two})] == ["A", "B"],
          "a dict registry gives its keys as ids")
    check([i for i, _f in entries_of([f_one, f_two])] == ["f_one", "f_two"],
          "a list of functions gives the function names as ids")
    check([i for i, _f in entries_of([("X", f_one), ("Y", f_two)])] == ["X", "Y"],
          "a list of (id, function) pairs gives the ids as written")
    raises(lambda: entries_of({"A": 5}), "a registry value that is not callable is refused")
    raises(lambda: entries_of([("A", "B", "C")]), "a tuple that is not a pair is refused")
    raises(lambda: entries_of([7]), "a registry item with no name is refused")
    raises(lambda: entries_of([(5, f_one)]), "an id that is not a string is refused")
    raises(lambda: resolve_registry("nocolon"), "a registry spec with no colon is refused")
    raises(lambda: resolve_registry(":NAME"), "a registry spec with no module is refused")
    check(duplicate_ids(["a", "b", "a", "a", "c", "b"]) == ["a", "b"],
          "a repeated id is listed once, in first-seen order")
    check(duplicate_ids(["a", "b"]) == [], "a unique list repeats nothing")
    check(unknown_ids(["a", "z"], ["a", "b"]) == ["z"], "an id the registry lacks is named")
    check(check_command([]) is not None and check_command(["-m"]) is not None
          and check_command(["-m", "unittest"]) is None and check_command(["t.py"]) is None,
          "a test command needs something to run, and -m needs a module")

    # ---- the verdict on one run, every status
    check(classify(1, 3, False, False) == PROVEN, "stub called and tests failed is PROVEN")
    check(classify(0, 3, False, False) == SURVIVED,
          "stub called and tests passed is SURVIVED")
    check(classify(0, 0, False, False) == UNCALLED, "stub never called and tests passed is UNCALLED")
    check(classify(1, 0, False, False) == SUSPECT,
          "tests failed without calling the stub is SUSPECT, not PROVEN")
    check(classify(None, None, True, False) == TIMEOUT, "a timeout is TIMEOUT")
    check(classify(1, None, False, False) == UNKNOWN,
          "no report of calls is UNKNOWN even when the tests failed")
    check(classify(0, None, False, True) == UNSTUBBABLE, "a refused stub is UNSTUBBABLE")
    check(classify(1, 5, True, False) == TIMEOUT,
          "a timeout outranks the hits it reported before it was cut off")
    for status in STATUSES:
        check(len(explain(status, 1, 2, 9, "why")) > 0,
              "a %s run explains itself in a line" % status)
    check(explain(UNSTUBBABLE, 1, None, 9, "because") == "because",
          "the reason an UNSTUBBABLE check was refused is passed through")

    # ---- the gate
    def res(ident, status):
        return {"id": ident, "status": status}

    code, lines = verdict([], [])
    check(code == EXIT_NOTHING and "NOTHING CHECKED" in lines[0],
          "an empty registry exits 4 and does not call itself clean  <-- pinned defect")
    code, lines = verdict([res("A", PROVEN), res("B", PROVEN)], [])
    check(code == 0 and "2 of 2" in lines[-1], "every check proven exits 0")
    code, lines = verdict([res("A", PROVEN), res("B", SURVIVED)], [])
    check(code == 1 and "B" in lines[-1] and "1 of 2" in lines[-1],
          "one check not proven exits 1 and names it")
    code, lines = verdict([res("A", PROVEN), res("B", UNCALLED)], ["B"])
    check(code == 0 and "allowed to stay unproven: B" in lines[0]
          and "1 allowed unproven" in lines[-1],
          "an allowed check that is not proven does not fail the run")
    code, lines = verdict([res("A", PROVEN)], ["A"])
    check(code == 0 and "drop them from --allow: A" in lines[0],
          "an allowed check that is proven is reported as a stale allowance")
    code, lines = verdict([res("A", SUSPECT), res("B", TIMEOUT)], ["B"])
    check(code == 1 and "A" in lines[-1] and "not proven: A." in lines[-1],
          "an allowance covers only the id it names")
    counts = summarize([res("A", PROVEN), res("B", PROVEN), res("C", UNCALLED)])
    check(counts[PROVEN] == 2 and counts[UNCALLED] == 1 and sum(counts.values()) == 3
          and set(counts) == set(STATUSES),
          "every status is counted and the counts add up to the checks")
    report = json.loads(render_report([res("A", PROVEN)], counts))
    check(report["checks"][0]["id"] == "A" and report["counts"][PROVEN] == 2,
          "the report carries the ids, statuses and counts")
    check(tail_of("a\n\n b  \nc\nDEADCHECK-HITS 1\n", 2) == [" b", "c"] and tail_of("a\nb", 0) == []
          and tail_of("a\nb", 9) == ["a", "b"] and len(tail_of("x" * 400, 1)[0]) == 160,
          "the output tail skips blank lines and cuts long ones")

    # ---- apply_stub, in this process
    plain, Holder = targets["plain"], targets["Holder"]
    with_default, generator = targets["with_default"], targets["generator"]
    sys.modules[_PROBE] = types.ModuleType(_PROBE)
    sys.modules[_PROBE].hits = {}
    try:
        apply_stub(plain, "P-1", [])
        check(plain([-1, -2]) == [] and sys.modules[_PROBE].hits == {"P-1": 1},
              "a stubbed function returns the literal and counts the call")
        first = plain([1])
        second = plain([1])
        check(first == [] and first is not second,
              "every call returns a fresh literal, not one shared list")
        apply_stub(with_default, "P-2", None)
        check(with_default([1]) is None and with_default([1], False, extra=9) is None,
              "a function with defaults and keyword-only arguments takes any call")
        holder = Holder()
        apply_stub(holder.method, "P-3", [])
        check(holder.method([-1]) == [] and Holder().method([-1]) == [],
              "a bound method is stubbed through its function")

        closure = targets["outer"]()
        apply_stub(closure, "P-4", [])
        check(closure([5]) == [] and len(closure.__code__.co_freevars) == 1,
              "a function with a closure is stubbed and keeps its free variable")

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            apply_stub(generator, "P-5", None)
        check(list(generator([1, 2])) == [] and inspect.isgeneratorfunction(generator)
              and sys.modules[_PROBE].hits["P-5"] == 1,
              "a generator function is stubbed with a generator that yields nothing and "
              "Python raises no deprecation warning  <-- pinned defect")

        def refusal(fn):
            """The reason apply_stub refuses fn, or "" when it stubs it."""
            try:
                apply_stub(fn, "Z", [])
            except Unstubbable as exc:
                return str(exc)
            return ""

        check("awaits" in refusal(targets["coroutine"]),
              "a coroutine function is refused with a reason")
        check("awaits" in refusal(targets["agen"]),
              "an async generator function is refused with a reason")
        check("builtin_function_or_method" in refusal(len),
              "a builtin is refused with a reason")
        check("Holder" in refusal(Holder()), "a callable object is refused with a reason")
        check(refusal(targets["f_two"]) == "", "a plain function is not refused")
    finally:
        sys.modules.pop(_PROBE, None)

    # ---- the whole tool, against a synthetic project, in real child processes
    project = make("proj", {"checks.py": _FIXTURE_CHECKS, "test_checks.py": _FIXTURE_TESTS})
    base = ["--registry", "checks:CHECKS", "--cwd", project]
    unit = ["--", "-m", "unittest", "discover", "-s", project, "-t", project]
    with environ(PYTHONDONTWRITEBYTECODE=None):
        code, out, err = cli(base + unit)

    def status_of(text, ident):
        """The status printed for ident in the output text, or None."""
        for line in text.splitlines():
            if line.split()[1:2] == [ident]:
                return line.split()[0]
        return None
    check(status_of(out, "RULE-99") is None, "a status is None for an id that was not listed")
    check(code == 1, "a registry with an unproven check exits 1")
    check("holds 6 check(s), 6 selected" in out and "baseline: the test command passed" in out,
          "the run names the registry and passes its baseline first")
    check(status_of(out, "RULE-01") == PROVEN,
          "a check a test needs to fire is PROVEN, found by a direct import too"
          "")
    check(status_of(out, "RULE-02") == SURVIVED,
          "a check only a clean-sample test runs is SURVIVED, the case a clean-sample test leaves open"
          "")
    check(status_of(out, "RULE-03") == UNCALLED, "a check no test runs is UNCALLED")
    check(status_of(out, "RULE-04") == PROVEN,
          "a check behind a closure wrapper is PROVEN, not skipped")
    check(status_of(out, "RULE-05") == UNSTUBBABLE and status_of(out, "RULE-06") == UNSTUBBABLE,
          "an async check and a callable object are named UNSTUBBABLE")
    check("checks stubbed: 6 of 6" in out and "  PROVEN       2" in out
          and "  UNSTUBBABLE  2" in out, "the counts add up to the checks stubbed")
    check("VERDICT: 2 of 6 check(s) proven able to fire, 4 not proven: RULE-02, "
          "RULE-03, RULE-05, RULE-06." in out, "the verdict names every unproven check")
    check(err == "", "a normal run writes nothing to stderr")
    check(not os.path.exists(os.path.join(project, "__pycache__")),
          "the tests ran with no __pycache__ written into the project  <-- pinned defect")

    code, out, err = cli(base + ["--only", "RULE-01", "--only", "RULE-04"] + unit)
    check(code == 0 and "2 selected" in out and "checks stubbed: 2 of 6" in out
          and "RULE-02" not in out, "--only stubs just the named checks")
    code, out, err = cli(base + ["--allow", "RULE-02", "--allow", "RULE-03",
                                 "--allow", "RULE-05", "--allow", "RULE-06"] + unit)
    check(code == 0 and "allowed to stay unproven: RULE-02, RULE-03, RULE-05, RULE-06" in out,
          "--allow lets named unproven checks through and says so")
    code, out, err = cli(base + ["--allow", "RULE-01", "--only", "RULE-01"] + unit)
    check(code == 0 and "drop them from --allow: RULE-01" in out,
          "an allowance on a proven check is reported stale")
    code, out, err = cli(base + ["--only", "RULE-77"] + unit)
    check(code == 64 and "not registered: RULE-77" in err, "--only with an unknown id exits 64")
    code, out, err = cli(base + ["--allow", "RULE-77"] + unit)
    check(code == 64 and "not registered: RULE-77" in err,
          "--allow with an unknown id exits 64, so a typo cannot waive nothing")

    with environ(PYTHONSAFEPATH="1"):
        code, out, err = cli(base + ["--only", "RULE-01"] + unit)
    check(code == 0 and status_of(out, "RULE-01") == PROVEN,
          "the registry imports even when Python is told not to put the folder on its path"
          "  <-- pinned defect")
    code, out, err = cli(base + ["--stub-value", "None", "--only", "RULE-01"] + unit)
    check(code == 0 and "stub value None" in out and status_of(out, "RULE-01") == PROVEN,
          "--stub-value changes what the stub returns")

    # a script instead of -m, and an explicit interpreter
    write(project, "run_tests.py",
                   "import sys, unittest\n"
                   "sys.exit(0 if unittest.main(module='test_checks', exit=False)"
                   ".result.wasSuccessful() else 1)\n")
    code, out, err = cli(base + ["--python", sys.executable, "--only", "RULE-01", "--",
                                 "run_tests.py"])
    check(code == 0 and status_of(out, "RULE-01") == PROVEN,
          "a script path runs as the test command, with an explicit interpreter")

    # ---- the report is written only by --apply
    report_path = os.path.join(tmp, "report.json")
    code, out, err = cli(base + ["--only", "RULE-01", "--out", report_path] + unit)
    check(code == 0 and not os.path.exists(report_path) and "was not written" in out,
          "--out without --apply writes nothing at all  <-- pinned defect")
    code, out, err = cli(base + ["--only", "RULE-01", "--out", report_path, "--apply"] + unit)
    with open(report_path) as handle:
        written = json.load(handle)
    check(code == 0 and written["checks"][0]["id"] == "RULE-01"
          and written["checks"][0]["status"] == PROVEN, "--apply writes the report")
    code, out, err = cli(base + ["--only", "RULE-01", "--out", report_path, "--apply"] + unit)
    check(code == 64 and "already exists" in err,
          "--out will not replace a file that is there, on a dry run too")
    code, out, err = cli(base + ["--only", "RULE-01", "--out",
                                 os.path.join(project, "checks.py")] + unit)
    check(code == 64, "--out naming a source file is refused before --apply is added"
          "  <-- pinned defect")
    code, out, err = cli(base + ["--only", "RULE-01", "--out",
                                 os.path.join(tmp, "nodir", "r.json"), "--apply"] + unit)
    check(code == 2 and "error:" in err, "a report that cannot be written exits 2")
    code, out, err = cli(base + ["--apply"] + unit)
    check(code == 64 and "--apply needs --out" in err, "--apply without --out is a usage error")

    # ---- usage errors and the abbreviation defect
    for bad, label in ((["--timeout", "0"], "--timeout of zero"),
                       (["--tail", "-1"], "a negative --tail"),
                       (["--stub-value", "open('x')"], "a --stub-value that is code"),
                       (["--cwd", os.path.join(tmp, "nodir")], "a --cwd that is not a folder")):
        code, out, err = cli(["--registry", "checks:CHECKS"] + bad + unit)
        check(code == 64 and out == "", "%s is a usage error and runs nothing" % label)
    code, out, err = cli(unit)
    check(code == 64 and "--registry is required" in err, "no --registry is a usage error")
    code, out, err = cli(["--registry", "checks:CHECKS"])
    check(code == 64 and "name the test command" in err, "no test command is a usage error")
    code, out, err = cli(["--registry", "checks:CHECKS", "--", "-m"])
    check(code == 64 and "-m needs a module" in err, "-m with no module is a usage error")
    code, out, err = cli(["--bogus"])
    check(code == 64, "an unknown flag exits 64, not argparse's 2, which means I/O here")
    code, out, err = cli(["--help"])
    check(code == 0 and "--registry" in out, "--help exits 0")
    check(_parse(["--registry", "a:B", "--", "-m", "x"]).command == ["-m", "x"],
          "the test command after -- is read whole, flags included")
    check(_parse(["--registry", "a:B"]).apply is False, "--apply defaults to OFF")
    for prefix, label in (("--ap", "--apply"), ("--regi", "--registry"), ("--stub", "--stub-value"),
                          ("--al", "--allow"), ("--ou", "--out")):
        refused_code, refused_out, refused_err = cli(["--registry", "a:B", prefix, "x", "--",
                                                      "t.py"])
        check(refused_code == 64 and "unrecognized arguments" in refused_err,
              "a prefix of %s is refused, not read as the flag" % label
              + ("  <-- pinned defect" if label == "--apply" else ""))
    written_before = os.path.exists(os.path.join(tmp, "x"))
    check(not written_before, "a refused prefix of --apply wrote nothing")

    # ---- refusals
    code, out, err = cli(["--registry", "nothere:CHECKS", "--cwd", project] + unit)
    check(code == 3 and "could not be loaded" in out and "ModuleNotFoundError" in out,
          "a registry module that does not import is REFUSED, exit 3")
    code, out, err = cli(["--registry", "checks:NOPE", "--cwd", project] + unit)
    check(code == 3 and "AttributeError" in out, "a registry name that is missing is REFUSED")
    code, out, err = cli(["--registry", "checks", "--cwd", project] + unit)
    check(code == 3 and "module:NAME" in out, "a registry spec with no colon is REFUSED")
    exiting = make("exiting", {"reg.py": "import sys\nsys.exit(5)\n"})
    code, out, err = cli(["--registry", "reg:X", "--cwd", exiting, "--", "-m", "unittest"])
    check(code == 3 and "SystemExit" in out,
          "a registry module that calls sys.exit is REFUSED, not obeyed")
    dupes = make("dupes", {
        "reg.py": "def a(r): return []\ndef b(r): return []\nCHECKS = [('X', a), ('X', b)]\n"})
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", dupes, "--", "-m", "unittest"])
    check(code == 3 and "id(s) on more than one entry: X" in out and "RULE" not in out,
          "two checks with one id are REFUSED before anything is stubbed  <-- pinned defect")
    empty = make("empty", {"reg.py": "CHECKS = {}\n"})
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", empty, "--", "-m", "unittest"])
    check(code == 4 and "NOTHING CHECKED" in out,
          "an empty registry exits 4, because a run that checked nothing is not clean"
          "  <-- pinned defect")
    red = make("red", {
        "reg.py": "def a(r): return [1]\nCHECKS = {'A': a}\n",
        "test_red.py": "import unittest\nclass T(unittest.TestCase):\n"
                       "    def test_fails(self):\n        self.fail('red')\n"})
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", red, "--tail", "2", "--", "-m",
                          "unittest", "discover", "-s", red, "-t", red])
    check(code == 3 and "fails without any stub" in out and "A  " not in out
          and "|" in out, "a red baseline is REFUSED, and the tail shows why")
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", red, "--", "missing_script.py"])
    check(code == 3 and "|" not in out, "a missing test script is a red baseline, REFUSED")

    # ---- statuses that need a test run to behave differently under a stub
    counter = os.path.join(tmp, "counter.txt")
    flaky = make("flaky", {
        "reg.py": "def a(r): return [1]\nCHECKS = {'A': a}\n",
        "test_flaky.py": "import os, unittest\n"
                         "PATH = %r\n"
                         "class T(unittest.TestCase):\n"
                         "    def test_flaky(self):\n"
                         "        n = int(open(PATH).read()) if os.path.exists(PATH) else 0\n"
                         "        open(PATH, 'w').write(str(n + 1))\n"
                         "        self.assertLess(n, 1)\n" % counter})
    flaky_cmd = ["--", "-m", "unittest", "discover", "-s", flaky, "-t", flaky]
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", flaky, "--tail", "6"] + flaky_cmd)
    check(code == 1 and "SUSPECT      A" in out and "without calling the stub" in out
          and "AssertionError" in out,
          "tests that fail without calling the stub are SUSPECT, not PROVEN, with the tail"
          "")

    sleeper = make("sleeper", {
        "reg.py": "def a(r): return [1]\nCHECKS = {'A': a}\n",
        "t_sleep.py": "import os, sys, time\n"
                      "PATH = %r\n"
                      "n = int(open(PATH).read()) if os.path.exists(PATH) else 0\n"
                      "open(PATH, 'w').write(str(n + 1))\n"
                      "if n >= 1:\n    time.sleep(60)\n" % (counter + ".sleep")})
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", sleeper, "--timeout", "4", "--",
                          "t_sleep.py"])
    check(code == 1 and "TIMEOUT      A" in out and "past 4.0 seconds" in out,
          "a test run past --timeout is TIMEOUT, and the run ends")

    quitter = make("quitter", {
        "reg.py": "def a(r): return [1]\nCHECKS = {'A': a}\n",
        "t_quit.py": "import os\nos._exit(0)\n"})
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", quitter, "--", "t_quit.py"])
    check(code == 1 and "UNKNOWN      A" in out,
          "tests that end without reporting are UNKNOWN even at exit 0")

    slow_baseline = make("slow", {
        "reg.py": "def a(r): return [1]\nCHECKS = {'A': a}\n",
        "t_slow.py": "import time\ntime.sleep(60)\n"})
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", slow_baseline, "--timeout", "3",
                          "--", "t_slow.py"])
    check(code == 3 and "past 3.0 seconds" in out, "a baseline past --timeout is REFUSED")

    # ---- a registry that cannot be listed in time, and an interpreter that does not exist
    hangs = make("hangs", {"reg.py": "import time\ntime.sleep(60)\n"})
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", hangs, "--timeout", "3",
                          "--", "t.py"])
    check(code == 3 and "listing ran past 3.0 seconds" in out,
          "a registry import that hangs is REFUSED after --timeout")
    quiet = make("quiet", {"reg.py": "import os\nos._exit(0)\n"})
    code, out, err = cli(["--registry", "reg:CHECKS", "--cwd", quiet, "--", "t.py"])
    check(code == 3 and "no answer" in out, "a registry listing that ends with no answer is REFUSED")
    code, out, err = cli(base + ["--python", os.path.join(tmp, "no-such-python")] + unit)
    check(code == 2 and "could not start" in err, "an interpreter that cannot start exits 2")

    # ---- the child, in this process, so the lines it runs are measured. Its
    # registry lives in memory and its tests are stdlib or this file, so coverage
    # never traces a file that is deleted before it writes its report.
    def in_memory(name, source):
        module = types.ModuleType(name)
        exec(compile(source, "<fixture %s>" % name, "exec"), module.__dict__)
        sys.modules[name] = module
        return module

    fixture = in_memory("deadcheck_fixture", _FIXTURE_CHECKS)
    try:
        job = {"mode": "list", "cwd": tmp, "registry": "deadcheck_fixture:CHECKS",
               "argv": ["-m", "this"], "value": "[]"}
        code, out = in_process(job)
        check(code == 0 and parse_ids(out) == ["RULE-0%d" % n for n in range(1, 7)],
              "the child lists the registered ids")
        code, out = in_process(dict(job, registry="deadcheck_fixture:Nope"))
        check(code == 1 and "AttributeError" in (find_marker(out, "ERROR") or ""),
              "the child reports a registry it cannot read")
        code, out = in_process(dict(job, mode="run", id="RULE-01"))
        check(code == 0 and parse_hits(out) == 0 and fixture.rule_one.__code__.co_filename == "<deadcheck-stub>",
              "the child stubs the check, runs the tests and reports no calls when none came")
        code, out = in_process(dict(job, mode="run", id="RULE-06"))
        check(code == 1 and "RuleSix" in (find_marker(out, "UNSTUBBABLE") or "")
              and "Zen of Python" not in out,
              "the child refuses a callable object and runs no tests")
        code, out = in_process(dict(job, mode="baseline",
                                    argv=[os.path.abspath(__file__), "--help"]))
        check(code == 0 and parse_hits(out) == 0 and "--registry" in out,
              "the baseline runs the test script with no stub and its folder on the path")
    finally:
        sys.modules.pop("deadcheck_fixture", None)
    check("DEADCHECK-ERROR caf?" in _capture_marker("ERROR", u"caf\u00e9"),
          "a marker is ASCII only, so a console that cannot encode never breaks it")

    # run_target exit codes, with runpy replaced so no file is needed
    real_runpy = (runpy.run_path, runpy.run_module)

    def raising(error):
        def fake(*args, **kwargs):
            if error is not None:
                raise error
        return fake
    for error, expected, label in (
            (SystemExit(), 0, "SystemExit with no code is exit 0"),
            (SystemExit(3), 3, "SystemExit(3) is exit 3"),
            (SystemExit("boom"), 1, "SystemExit with text is exit 1"),
            (RuntimeError("x"), 1, "an exception is exit 1"),
            (None, 0, "a script that returns is exit 0")):
        runpy.run_path = runpy.run_module = raising(error)
        saved = list(sys.argv)
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                got = (run_target(["script.py"]), run_target(["-m", "mod", "arg"]))
        finally:
            runpy.run_path, runpy.run_module = real_runpy
            sys.argv[:] = saved
        check(got == (expected, expected), label + ", for a script and for -m")

    # ---- the harness itself
    probe = _Harness()
    with contextlib.redirect_stdout(io.StringIO()):
        probe.check(False, "probe one")
        probe.check(True, "probe two")
        probe.raises(lambda: None, "probe three")
        probe.raises(lambda: 1 / 0, "probe four")
        probe.raises(lambda: parse_stub_value("("), "probe five")
    check(probe.passed == 2 and len(probe.failed) == 3 and probe.failed[:2] == [
        "probe one", "probe three (no error raised)"]
          and probe.failed[2].startswith("probe four (wrong exception"),
          "check() and raises() really do record a failure  <-- pinned defect")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red_status = probe.footer()
    check(red_status == 1 and "5 assertions, 3 failed" in buf.getvalue()
          and "  FAILED: probe one" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1  <-- pinned defect")
    with contextlib.redirect_stdout(io.StringIO()) as quiet_out:
        zero = _Harness().footer()
    check(zero == 0 and "0 assertions, 0 failed" in quiet_out.getvalue(),
          "an empty run reports zero failures and exits 0")

    # ---- an id the console cannot encode must not turn a verdict into a traceback
    wide = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict",
                              newline="\n")
    saved_out = sys.stdout
    sys.stdout = wide
    try:
        _tolerate_odd_characters()
        print(u"RULE-\u00e9 verdict")
        wide.flush()
        shown = wide.buffer.getvalue()
    finally:
        sys.stdout = saved_out
    check(shown == b"RULE-? verdict\n",
          "an id the console cannot encode prints as ? and the verdict still arrives"
          "  <-- pinned defect")

    # ---- main() turns a crash into 70, never the 1 a scheduler reads as a finding
    real_run = globals()["_run"]
    globals()["_run"] = lambda argv: 1 // 0
    try:
        code, out, err = cli([])
    finally:
        globals()["_run"] = real_run
    check(code == EXIT_CRASH and "unexpected ZeroDivisionError" in err,
          "an unexpected exception exits 70, not the 1 that means a check was not proven"
          "  <-- pinned defect")

    # ---- nothing leaked, nothing left behind
    canary = "DEADCHECK_CANARY_SECRET_9f3a"
    os.environ[canary] = "hunter2-canary"
    try:
        code, out, err = cli(base + ["--only", "RULE-01", "--tail", "5", "--out",
                                     os.path.join(tmp, "canary.json"), "--apply"] + unit)
    finally:
        os.environ.pop(canary, None)
    with open(os.path.join(tmp, "canary.json")) as handle:
        artefact = handle.read()
    check("hunter2-canary" not in out + err + artefact and canary not in out + err + artefact,
          "an environment secret reaches neither stdout, stderr nor the report")
    check(not [n for n in os.listdir(project) if n.endswith(".pyc") or n == "__pycache__"],
          "after every run the project folder holds no bytecode")

    # ---- no network: the module imports nothing that could open a socket
    with io.open(os.path.abspath(__file__), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.add(node.module.split(".")[0])
    check(len(imported_names) > 10 and not imported_names & set(
        ["socket", "ssl", "http", "urllib", "ftplib", "smtplib", "telnetlib", "requests",
         "asyncio", "xmlrpc"]),
          "the module imports no network library, so there is no credential in it to leak")

    # ---- importing the module runs nothing
    spec = importlib.util.spec_from_file_location("deadcheck_import_probe", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = before
    check(buf.getvalue() == "" and imported.classify(1, 1, False, False) == PROVEN,
          "importing the module prints nothing and exposes the core")

    return harness.footer()


def _capture_marker(name, text):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _marker(name, text)
    return buf.getvalue()


def self_test():
    """Run the assertions in a temp directory that is removed even when one
    of them raises, so a crash never leaves deadcheck-selftest-* behind."""
    print("deadcheck self-test: temporary files and child Python processes only, "
          "no network, no credentials")
    print("-" * 68)
    tmp = tempfile.mkdtemp(prefix="deadcheck-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
