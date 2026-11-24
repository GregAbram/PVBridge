#!/usr/bin/env python3
"""run_tests -- score models on the ParaView voice-request test set.

    run_tests.py -m reference            # self-check: every case should pass
    run_tests.py -m opus -m local        # compare models (same cases, same order)
    run_tests.py -m local --only bg_black,viridis
    run_tests.py -m local --cat basic
    run_tests.py --report results/*.json # table from earlier runs

WARNING: this drives the live ParaView GUI and deletes its pipeline before each
case. The current state is saved first to ~/.pvbridge/testout/backup-<time>.pvsm
(File > Load State to get it back).

Results: results/<time>-<model>.json (every request, the code the model ran,
its reply, the check outcome) and a summary table on stdout.
"""
import argparse
import ast
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
import pvagent  # noqa: E402
from cases import CASES, HELPERS  # noqa: E402

RESULTS = os.path.join(HERE, "results")
# numbers in a reply: "3508", "3,508", "3 508" (incl. thin/no-break spaces), "1.256", "2.2e-05"
SEPS = "[,\u00a0\u202f\u2009 ]"
NUMBER = r"-?\d{1,3}(?:" + SEPS + r"\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?(?:[eE]-?\d+)?"


def pv(code, timeout=120):
    ok, out = pvagent.pvsend(code, timeout)
    if not ok:
        raise RuntimeError(out)
    return out


def run_check(case):
    body = "\n".join("    " + l for l in case["check"].splitlines())
    code = (HELPERS + "\ndef _t_check():\n" + body + "\n"
            "try:\n    _t_res = _t_check()\nexcept Exception as _e:\n    _t_res = (False, 'check raised: %r' % (_e,))\n"
            "('CHECK', bool(_t_res[0]), str(_t_res[1]))\n")
    out = pv(code)
    last = out.strip().splitlines()[-1]
    _, ok, detail = ast.literal_eval(last)
    return ok, detail


def run_case(agent, case):
    pv(HELPERS + "\n_t_setup(%r)" % case["setup"])
    expected = None
    if case.get("expect"):
        expected = float(pv(case["expect"]).strip().splitlines()[-1])
    agent.reset()
    if hasattr(agent.backend, "reference"):
        agent.backend.reference = case.get("reference")
    turns = []
    total = {"seconds": 0.0, "tool_calls": 0, "tool_errors": 0, "in": 0, "out": 0, "cache_read": 0}
    reply = ""
    for i, text in enumerate(case["turns"]):
        if hasattr(agent.backend, "reference") and i < len(case["turns"]) - 1:
            continue            # reference code covers the whole conversation in one go
        try:
            reply, st = agent.ask(text)
        except Exception as e:
            reply, st = "EXCEPTION: %s" % e, {"seconds": 0, "tool_calls": 0, "tool_errors": 1, "usage": {}}
        turns.append({"request": text, "reply": reply, "trace": agent.trace, **st})
        for k in ("seconds", "tool_calls", "tool_errors"):
            total[k] += st.get(k, 0)
        for k, val in (st.get("usage") or {}).items():
            total[k] = total.get(k, 0) + val
    ok, detail = run_check(case)
    reply_ok = True
    if case.get("reply_re"):
        reply_ok = bool(re.search(case["reply_re"], reply, re.I | re.S))
        if not reply_ok:
            detail = "reply didn't match /%s/" % case["reply_re"]
    if expected is not None:
        nums = [float(re.sub(SEPS, "", x)) for x in re.findall(NUMBER, reply)]
        reply_ok = reply_ok and any(abs(x - expected) <= 0.02 * abs(expected) for x in nums)
        detail = "expected %g, reply numbers %s" % (expected, nums[:6])
    return {"id": case["id"], "cat": case["cat"], "pass": bool(ok and reply_ok),
            "detail": detail, "turns": turns, **{k: round(v, 2) for k, v in total.items()}}


def summarize(runs):
    """runs: list of {"model":..., "cases":[...]} -> printable table."""
    ids = []
    for r in runs:
        for c in r["cases"]:
            if c["id"] not in ids:
                ids.append(c["id"])
    names = [r["model"] for r in runs]
    w = max(len(i) for i in ids) + 2
    lines = ["%-*s %-13s" % (w, "case", "category") + "".join("%-14s" % n for n in names)]
    by = [{c["id"]: c for c in r["cases"]} for r in runs]
    cats = {}
    for i in ids:
        cat = next(b[i]["cat"] for b in by if i in b)
        cells = []
        for b in by:
            c = b.get(i)
            cells.append("-" if c is None else ("PASS" if c["pass"] else "fail") + " %5.1fs" % c["seconds"])
        lines.append("%-*s %-13s" % (w, i, cat) + "".join("%-14s" % x for x in cells))
    lines.append("")
    for r in runs:
        cs = r["cases"]
        n = sum(c["pass"] for c in cs)
        per = {}
        for c in cs:
            p = per.setdefault(c["cat"], [0, 0])
            p[0] += c["pass"]
            p[1] += 1
        secs = sorted(c["seconds"] for c in cs)
        med = secs[len(secs) // 2] if secs else 0
        lines.append("%s: %d/%d passed; median %.1fs per case; %s" % (
            r["model"], n, len(cs), med, ", ".join("%s %d/%d" % (k, a, b) for k, (a, b) in per.items())))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--model", action="append", help="model name from models.json (repeatable)")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--cat", help="comma-separated categories")
    ap.add_argument("--report", nargs="*", help="print the table for saved result files and exit")
    ap.add_argument("--no-backup", action="store_true")
    a = ap.parse_args()

    if a.report is not None:
        runs = [json.load(open(p)) for p in (a.report or sorted(glob.glob(os.path.join(RESULTS, "*.json"))))]
        print(summarize(runs))
        return
    if not a.model:
        ap.error("give at least one -m MODEL")

    cases = CASES
    if a.only:
        want = a.only.split(",")
        cases = [c for c in cases if c["id"] in want]
    if a.cat:
        cases = [c for c in cases if c["cat"] in a.cat.split(",")]

    try:
        pv("1", timeout=10)
    except RuntimeError as e:
        sys.exit("ParaView bridge not answering: %s" % e)
    if not a.no_backup:
        p = pv(HELPERS + "\n_p = os.path.join(_T_OUT, 'backup-' + __import__('time').strftime('%Y%m%d-%H%M%S') + '.pvsm'); SaveState(_p); _p")
        print("saved current ParaView state to", p.strip().strip("'"))

    os.makedirs(RESULTS, exist_ok=True)
    runs = []
    for model in a.model:
        agent = pvagent.Agent(model)
        print("\n=== %s: %d cases" % (model, len(cases)))
        results = []
        for case in cases:
            try:
                r = run_case(agent, case)
            except Exception as e:
                r = {"id": case["id"], "cat": case["cat"], "pass": False, "detail": "harness error: %s" % e,
                     "turns": [], "seconds": 0, "tool_calls": 0, "tool_errors": 0}
            results.append(r)
            print("  %-18s %s  %5.1fs  %d calls  %s" % (r["id"], "PASS" if r["pass"] else "fail",
                                                      r["seconds"], r["tool_calls"],
                                                      "" if r["pass"] else str(r["detail"])[:110]), flush=True)
        run = {"model": model, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": results}
        path = os.path.join(RESULTS, "%s-%s.json" % (time.strftime("%Y%m%d-%H%M%S"), model))
        with open(path, "w") as f:
            json.dump(run, f, indent=1, default=str)
        print("  saved", path)
        runs.append(run)
    print("\n" + summarize(runs))


if __name__ == "__main__":
    main()
