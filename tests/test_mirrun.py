"""Unit tests for the MIR corpus supervisor (mirrun.py).

Everything here runs against a fake Python driver selected with ``--driver``;
no LLVM build or real corpus is required.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import testlib
from x86_mir_corpus import mirrun


FAKE_DRIVER = r'''#!/usr/bin/env python3
import json
import os
import signal
import sys
import time


def main():
    scenario_path = os.environ.get("FAKE_DRIVER_SCENARIO")
    scenario = {}
    if scenario_path and os.path.exists(scenario_path):
        with open(scenario_path, "r", encoding="utf-8") as handle:
            scenario = json.load(handle)
    in_path = os.environ["X86_LEGALIZER_MIR_CORPUS_IN"]
    out_path = os.environ["X86_LEGALIZER_MIR_OUT"]
    shard = os.environ.get("X86_LEGALIZER_MIR_SHARD", "0/1")
    rules = os.environ.get("X86_LEGALIZER_MIR_RULES", "production")
    max_steps = int(os.environ.get("X86_LEGALIZER_MIR_MAX_STEPS", "10000"))
    start_index = int(os.environ.get("X86_LEGALIZER_MIR_START_INDEX", "0"))

    with open(in_path, "r", encoding="utf-8") as handle:
        corpus = json.load(handle)
    atoms = corpus.get("atoms") or []
    shard_index, shard_count = (int(part) for part in shard.split("/"))
    selected = [(pos, atom) for pos, atom in enumerate(atoms)
                if pos % shard_count == shard_index and pos >= start_index]

    trace_path = os.environ.get("FAKE_DRIVER_TRACE")
    if trace_path:
        with open(trace_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "shard": shard,
                "in": in_path,
                "atom_count": len(atoms),
                "ids": [atom.get("id") for _, atom in selected],
                "rules": rules,
                "max_steps": max_steps,
                "start_index": start_index,
                "args": sys.argv[1:],
            }, sort_keys=True) + "\n")

    out = open(out_path, "w", encoding="utf-8")

    sleep_before_start = scenario.get("sleep_before_start") or 0
    if sleep_before_start:
        out.flush()
        time.sleep(sleep_before_start)

    def emit(obj):
        out.write(json.dumps(obj, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())

    plans = scenario.get("atoms") or {}
    default = scenario.get("default") or {
        "action": "normal", "outcome": "legalized", "steps": 1}

    for pos, atom in selected:
        atom_id = atom.get("id")
        plan = plans.get(atom_id, default)
        action = plan.get("action", "normal")
        if action == "silent":
            continue
        if action == "stall_before_start":
            time.sleep(30)
        if action == "abort_before_start":
            sys.stderr.write("Assertion failed: before start\n")
            sys.stderr.flush()
            os.kill(os.getpid(), signal.SIGABRT)
        emit({"event": "start", "id": atom_id, "index": pos})
        if action in ("normal", "budget", "budget_abort", "segv", "abort",
                      "exit_nonzero", "slow_legalize"):
            emit({"event": "constructed"})
            emit({"event": "legalize"})
        elif action in ("slow_construct", "construct_crash"):
            emit({"event": "constructed"})
        else:
            raise SystemExit("unknown fake driver action: " + action)
        out.flush()
        os.fsync(out.fileno())

        if action == "normal":
            event = {
                "event": "outcome",
                "id": atom_id,
                "index": pos,
                "opcode": atom.get("opcode"),
                "outcome": plan.get("outcome", "legalized"),
                "steps": plan.get("steps", 1),
                "detail_code": plan.get("detail_code", ""),
                "detail": plan.get("detail", ""),
            }
            if "failed_on_opcode" in plan:
                event["failed_on_opcode"] = plan["failed_on_opcode"]
            emit(event)
        elif action == "slow_construct":
            time.sleep(30)
        elif action == "slow_legalize":
            time.sleep(30)
        elif action == "construct_crash":
            sys.stderr.write("simulated construction crash\n")
            sys.stderr.flush()
            os.kill(os.getpid(), signal.SIGSEGV)
        elif action == "segv":
            sys.stderr.write("simulated segv\n")
            sys.stderr.flush()
            os.kill(os.getpid(), signal.SIGSEGV)
        elif action == "abort":
            sys.stderr.write("Assertion failed: simulated\n")
            sys.stderr.flush()
            os.kill(os.getpid(), signal.SIGABRT)
        elif action in ("budget", "budget_abort"):
            sys.stderr.write(
                "LLVM ERROR: legalizer step budget exceeded: "
                "atom=%s steps=%d\n" % (atom_id, max_steps + 1))
            sys.stderr.flush()
            if action == "budget":
                os._exit(1)
            os.kill(os.getpid(), signal.SIGABRT)
        elif action == "exit_nonzero":
            sys.stderr.write("driver failed\n")
            sys.stderr.flush()
            os._exit(3)

    summary = {"event": "summary", "atom_count": len(atoms),
               "rules": rules, "shard": shard}
    summary.update(scenario.get("summary") or {})
    emit(summary)
    sleep_after = scenario.get("sleep_after") or 0
    if sleep_after:
        time.sleep(sleep_after)
    out.close()
    sys.exit(scenario.get("exit", 0))


main()
'''


def make_atom(index, config="C2", rule_set="X86_ADD", opcode="G_ADD",
              types=("s32",), expected="Legal"):
    return {
        "id": f"atom/{index}",
        "origin": "test",
        "rule_set": rule_set,
        "opcode": opcode,
        "rule_index": None,
        "config": config,
        "query": {"types": list(types)},
        "expected": {"action": expected},
    }


def make_corpus(count, config="C2", **atom_kwargs):
    atoms = [make_atom(index, config=config, **atom_kwargs)
             for index in range(count)]
    return {
        "schema": "llvm-x86-legalizer-cpp-corpus",
        "version": 2,
        "config": config,
        "config_descriptor": {"id": config, "features": {}},
        "atom_count": count,
        "atoms": atoms,
    }


def make_result(index, outcome, steps=0, detail_code="", detail="",
                reason=None):
    result = {
        "id": f"atom/{index}",
        "index": index,
        "opcode": "G_ADD",
        "outcome": outcome,
        "steps": steps,
        "detail_code": detail_code,
        "detail": detail,
        "stderr_tail": "",
    }
    if reason is not None:
        result["reason"] = reason
    return result


class MirRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.driver = self.path("fake_driver.py")
        with open(self.driver, "w", encoding="utf-8") as handle:
            handle.write(FAKE_DRIVER)
        os.chmod(self.driver, 0o755)
        self.scenario_path = self.path("scenario.json")
        self.trace_path = self.path("trace.jsonl")
        self.write_scenario({})

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write_json(self, name, obj):
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(obj, handle)
        return path

    def write_corpus(self, corpus, name="corpus.json"):
        return self.write_json(name, corpus)

    def write_scenario(self, scenario):
        return self.write_json("scenario.json", scenario)

    def environ(self):
        env = os.environ.copy()
        env["FAKE_DRIVER_SCENARIO"] = self.scenario_path
        env["FAKE_DRIVER_TRACE"] = self.trace_path
        return env

    def run_cli(self, *args, env=None):
        return testlib.run_cli("mir-run", *args,
                                env=env or self.environ())

    def read_json(self, name):
        with open(self.path(name), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def read_trace(self):
        if not os.path.exists(self.trace_path):
            return []
        with open(self.trace_path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def report_atom(self, report, atom_id):
        for atom in report["atoms"]:
            if atom["id"] == atom_id:
                return atom
        self.fail(f"missing atom {atom_id}")

    def run_ok(self, corpus, *extra, scenario=None, expected=0):
        corpus_path = self.write_corpus(corpus)
        if scenario is not None:
            self.write_scenario(scenario)
        out = self.path("report.json")
        proc = self.run_cli("--corpus", corpus_path, "--driver", self.driver,
                            "--out", out, *extra)
        self.assertEqual(proc.returncode, expected, proc.stderr)
        return self.read_json("report.json"), proc

    # -- corpus pre-flight ---------------------------------------------------

    def test_validate_corpus_rejects_pointer_width_mismatch(self):
        corpus = make_corpus(1, config="C0")
        corpus["atoms"][0]["query"]["types"] = ["v2p0:64"]
        with self.assertRaises(mirrun.MirRunInfraError):
            mirrun.validate_corpus(corpus)

    def test_validate_corpus_rejects_atom_config_mismatch(self):
        corpus = make_corpus(1, config="C2")
        corpus["atoms"][0]["config"] = "C0"
        with self.assertRaises(mirrun.MirRunInfraError):
            mirrun.validate_corpus(corpus)

    def test_validate_corpus_rejects_unknown_config(self):
        corpus = make_corpus(1, config="C9")
        corpus["config_descriptor"]["id"] = "C9"
        with self.assertRaises(mirrun.MirRunInfraError):
            mirrun.validate_corpus(corpus)

    def test_validate_corpus_rejects_config_descriptor_disagreement(self):
        corpus = make_corpus(1, config="C2")
        corpus["config"] = "C1"
        with self.assertRaises(mirrun.MirRunInfraError):
            mirrun.validate_corpus(corpus)

    # -- sharding / only-atom ------------------------------------------------

    def test_partition_shards_maps_atoms_in_one_pass(self):
        atoms = [make_atom(index) for index in range(20000)]
        started = time.monotonic()
        buckets = mirrun.partition_shards(atoms, len(atoms))
        elapsed = time.monotonic() - started
        self.assertEqual(len(buckets), len(atoms))
        for pos, bucket in enumerate(buckets):
            self.assertEqual(bucket, [(pos, atoms[pos])])
        self.assertLess(elapsed, 5.0)

    def test_shard_mapping_and_merge(self):
        corpus = make_corpus(5)
        report, proc = self.run_ok(
            corpus, "--shard-size", "2",
            scenario={"default": {"action": "normal",
                                  "outcome": "legalized", "steps": 2}})
        self.assertEqual([atom["id"] for atom in report["atoms"]],
                         [f"atom/{index}" for index in range(5)])
        self.assertTrue(all(atom["outcome"] == "legalized"
                            for atom in report["atoms"]))
        trace = self.read_trace()
        self.assertEqual([entry["shard"] for entry in trace],
                         ["0/3", "1/3", "2/3"])
        self.assertEqual([entry["ids"] for entry in trace],
                         [["atom/0", "atom/3"], ["atom/1", "atom/4"],
                          ["atom/2"]])
        expected_args = ["--gtest_also_run_disabled_tests",
                         "--gtest_filter=*RunCorpusThroughLegalizer"]
        self.assertTrue(all(entry["args"] == expected_args
                            for entry in trace), trace)

    def test_jobs_two_matches_jobs_one(self):
        corpus = make_corpus(6)
        corpus_path = self.write_corpus(corpus)
        self.write_scenario({"atoms": {
            "atom/0": {"action": "normal", "outcome": "legalized",
                       "steps": 1},
            "atom/1": {"action": "normal", "outcome": "failed",
                       "detail_code": "libcall"},
            "atom/2": {"action": "normal", "outcome": "unbuildable",
                       "detail": "phi"},
            "atom/3": {"action": "normal", "outcome": "legalized",
                       "steps": 3},
            "atom/4": {"action": "normal", "outcome": "failed",
                       "detail_code": "libcall"},
            "atom/5": {"action": "normal", "outcome": "legalized",
                       "steps": 5},
        }})
        out_one = self.path("jobs1.json")
        out_two = self.path("jobs2.json")
        one_proc = self.run_cli(
            "--corpus", corpus_path, "--driver", self.driver,
            "--shard-size", "2", "--jobs", "1", "--out", out_one)
        self.assertEqual(one_proc.returncode, 0, one_proc.stderr)
        two_proc = self.run_cli(
            "--corpus", corpus_path, "--driver", self.driver,
            "--shard-size", "2", "--jobs", "2", "--out", out_two)
        self.assertEqual(two_proc.returncode, 0, two_proc.stderr)
        one = self.read_json("jobs1.json")
        two = self.read_json("jobs2.json")
        self.assertEqual(one["atoms"], two["atoms"])
        self.assertEqual(one["counts"], two["counts"])
        self.assertEqual(one["first_anomaly"], two["first_anomaly"])
        self.assertEqual(one["a3"], two["a3"])

    def test_jobs_two_isolates_timeout_and_crash_shards(self):
        corpus = make_corpus(5)
        report, proc = self.run_ok(
            corpus, "--shard-size", "1", "--jobs", "2", "--timeout", "1",
            scenario={"atoms": {
                "atom/0": {"action": "normal", "outcome": "legalized",
                           "steps": 1},
                "atom/1": {"action": "segv"},
                "atom/2": {"action": "slow_legalize"},
                "atom/3": {"action": "normal", "outcome": "legalized",
                           "steps": 4},
                "atom/4": {"action": "normal", "outcome": "failed",
                           "detail_code": "libcall"},
            }}, expected=1)
        self.assertEqual(self.report_atom(report, "atom/0")["outcome"],
                         "legalized")
        self.assertEqual(self.report_atom(report, "atom/1")["outcome"],
                         "crash")
        self.assertEqual(self.report_atom(report, "atom/1")["detail_code"],
                         "crash:segv")
        self.assertIn("simulated segv",
                      self.report_atom(report, "atom/1")["stderr_tail"])
        self.assertEqual(self.report_atom(report, "atom/2")["outcome"],
                         "hang")
        self.assertEqual(self.report_atom(report, "atom/3")["outcome"],
                         "legalized")
        self.assertEqual(self.report_atom(report, "atom/3")["steps"], 4)
        self.assertEqual(self.report_atom(report, "atom/4")["outcome"],
                         "failed")
        # Shard stderr files are isolated: the healthy shard has no tail.
        self.assertEqual(self.report_atom(report, "atom/3")["stderr_tail"],
                         "")
        self.assertEqual(report["counts"]["legalized"], 2)
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["counts"]["crash"], 1)
        self.assertEqual(report["counts"]["hang"], 1)

    def test_jobs_must_be_positive(self):
        corpus_path = self.write_corpus(make_corpus(1))
        out = self.path("bad-jobs.json")
        proc = self.run_cli("--corpus", corpus_path, "--driver", self.driver,
                            "--jobs", "0", "--out", out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--jobs", proc.stderr)
        self.assertFalse(os.path.exists(out))

    def test_only_atom_writes_one_atom_temp_corpus(self):
        corpus = make_corpus(5)
        report, proc = self.run_ok(
            corpus, "--only-atom", "atom/3", "--shard-size", "2",
            scenario={"default": {"action": "normal",
                                  "outcome": "legalized", "steps": 1}})
        self.assertEqual(report["corpus"]["atoms"], 5)
        self.assertEqual(self.report_atom(report, "atom/3")["outcome"],
                         "legalized")
        for index in (0, 1, 2, 4):
            atom = self.report_atom(report, f"atom/{index}")
            self.assertEqual(atom["outcome"], "not_run")
            self.assertEqual(atom["reason"], "only-atom")
        self.assertEqual(report["counts"]["legalized"], 1)
        self.assertEqual(report["counts"]["not_run"], 4)
        self.assertEqual(report["only_atom"], "atom/3")
        trace = self.read_trace()
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["shard"], "0/1")
        self.assertEqual(trace[0]["atom_count"], 1)
        self.assertNotEqual(trace[0]["in"], corpus)
        self.assertEqual(trace[0]["ids"], ["atom/3"])

    def test_only_atom_exit_follows_run_atom(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus, "--only-atom", "atom/1",
            scenario={"atoms": {"atom/1": {"action": "normal",
                                           "outcome": "failed"}}})
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["counts"]["not_run"], 2)

    # -- normal / budget / crash / resume -----------------------------------

    def test_resume_after_budget_restarts_after_the_anomaly(self):
        corpus = make_corpus(5)
        report, proc = self.run_ok(
            corpus,
            scenario={"atoms": {"atom/1": {"action": "budget"}}},
            expected=1)
        trace = self.read_trace()
        self.assertEqual(len(trace), 2)
        self.assertEqual([entry["start_index"] for entry in trace], [0, 2])
        self.assertEqual(trace[1]["ids"], ["atom/2", "atom/3", "atom/4"])
        self.assertEqual(
            [(atom["id"], atom["outcome"]) for atom in report["atoms"]],
            [("atom/0", "legalized"), ("atom/1", "budget"),
             ("atom/2", "legalized"), ("atom/3", "legalized"),
             ("atom/4", "legalized")])

    def test_resume_after_crash(self):
        corpus = make_corpus(5)
        report, proc = self.run_ok(
            corpus,
            scenario={"atoms": {"atom/1": {"action": "segv"}}},
            expected=1)
        trace = self.read_trace()
        self.assertEqual(len(trace), 2)
        self.assertEqual([entry["start_index"] for entry in trace], [0, 2])
        self.assertEqual(self.report_atom(report, "atom/1")["outcome"],
                         "crash")
        self.assertEqual(self.report_atom(report, "atom/1")["detail_code"],
                         "crash:segv")
        for index in (0, 2, 3, 4):
            self.assertEqual(
                self.report_atom(report, f"atom/{index}")["outcome"],
                "legalized")

    def test_resume_no_progress_leaves_remaining_not_run(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus, "--timeout", "1",
            scenario={"atoms": {"atom/0": {"action": "slow_construct"}}},
            expected=2)
        self.assertEqual(len(self.read_trace()), 1)
        self.assertEqual(self.report_atom(report, "atom/0")["outcome"],
                         "not_run")
        self.assertEqual(self.report_atom(report, "atom/0")["reason"],
                         "slow_or_stuck_before_legalizer")
        for index in (1, 2):
            atom = self.report_atom(report, f"atom/{index}")
            self.assertEqual(atom["outcome"], "not_run")
            self.assertEqual(atom["reason"], "after_incomplete")

    def test_discard_placeholders_only_after_an_anomaly(self):
        results = [
            make_result(0, "not_run", reason="missing_outcome"),
            make_result(1, "legalized"),
            make_result(2, "budget", steps=10001),
            make_result(3, "not_run", reason="after_budget"),
        ]
        kept = mirrun._discard_placeholders_after_anomaly(results)
        self.assertEqual([result["index"] for result in kept], [0, 1, 2])

    def test_merge_never_overwrites_an_anomaly_with_not_run(self):
        merged = {}
        mirrun._merge_invocation_results(
            merged, [make_result(1, "crash", detail_code="crash:segv")])
        mirrun._merge_invocation_results(
            merged, [make_result(1, "not_run", reason="after_crash")])
        self.assertEqual(merged[1]["outcome"], "crash")

    def test_clean_run_preserves_outcomes(self):
        corpus = make_corpus(2)
        report, proc = self.run_ok(
            corpus,
            scenario={"atoms": {
                "atom/0": {"action": "normal", "outcome": "legalized",
                           "steps": 4},
                "atom/1": {"action": "normal", "outcome": "failed",
                           "steps": 0, "detail_code": "libcall"},
            }})
        self.assertEqual(self.report_atom(report, "atom/0")["steps"], 4)
        self.assertEqual(self.report_atom(report, "atom/1")["outcome"],
                         "failed")
        self.assertEqual(self.report_atom(report, "atom/1")["detail_code"],
                         "libcall")
        self.assertEqual(report["counts"]["legalized"], 1)
        self.assertEqual(report["counts"]["failed"], 1)

    def test_failed_on_opcode_is_preserved(self):
        corpus = make_corpus(1)
        report, proc = self.run_ok(
            corpus,
            scenario={"atoms": {
                "atom/0": {"action": "normal", "outcome": "failed",
                           "detail_code": "unable_to_legalize",
                           "failed_on_opcode": "G_IMPLICIT_DEF"},
            }})
        atom = self.report_atom(report, "atom/0")
        self.assertEqual(atom["outcome"], "failed")
        self.assertEqual(atom["failed_on_opcode"], "G_IMPLICIT_DEF")

    def test_budget_marker_marks_budget_and_parses_steps(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus, "--max-steps", "5",
            scenario={"atoms": {"atom/1": {"action": "budget"}}},
            expected=1)
        atom = self.report_atom(report, "atom/1")
        self.assertEqual(atom["outcome"], "budget")
        self.assertEqual(atom["steps"], 6)
        self.assertEqual(atom["detail_code"], "budget")
        self.assertIn("legalizer step budget exceeded", atom["stderr_tail"])
        rest = self.report_atom(report, "atom/2")
        self.assertEqual(rest["outcome"], "legalized")
        self.assertEqual(report["counts"]["budget"], 1)

    def test_budget_marker_wins_over_signal(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus, "--max-steps", "9",
            scenario={"atoms": {"atom/1": {"action": "budget_abort"}}},
            expected=1)
        atom = self.report_atom(report, "atom/1")
        self.assertEqual(atom["outcome"], "budget")
        self.assertEqual(atom["steps"], 10)
        self.assertEqual(report["counts"]["crash"], 0)

    def test_timeout_inside_legalizer_is_hang(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus, "--timeout", "1",
            scenario={"atoms": {"atom/1": {"action": "slow_legalize"}}},
            expected=1)
        atom = self.report_atom(report, "atom/1")
        self.assertEqual(atom["outcome"], "hang")
        self.assertEqual(atom["detail_code"], "hang:timeout")
        self.assertEqual(self.report_atom(report, "atom/2")["outcome"],
                         "legalized")
        self.assertEqual(report["counts"]["hang"], 1)

    def test_timeout_before_legalize_is_not_run_not_hang(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus, "--timeout", "1",
            scenario={"atoms": {"atom/1": {"action": "slow_construct"}}},
            expected=2)
        atom = self.report_atom(report, "atom/1")
        self.assertEqual(atom["outcome"], "not_run")
        self.assertEqual(atom["reason"], "slow_or_stuck_before_legalizer")
        self.assertEqual(report["counts"]["hang"], 0)
        self.assertTrue(report["infra_warnings"])
        self.assertEqual(self.report_atom(report, "atom/2")["outcome"],
                         "not_run")

    def test_slow_but_complete_shard_is_not_a_hang(self):
        corpus = make_corpus(2)
        report, proc = self.run_ok(
            corpus, "--timeout", "1",
            scenario={"default": {"action": "normal",
                                  "outcome": "legalized", "steps": 2},
                      "sleep_after": 2})
        self.assertTrue(all(atom["outcome"] == "legalized"
                            for atom in report["atoms"]))
        self.assertEqual(report["counts"]["hang"], 0)
        self.assertTrue(any("timed out afterwards" in warning
                            for warning in report["infra_warnings"]))

    def test_construction_signal_is_crash_construction(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus,
            scenario={"atoms": {"atom/1": {"action": "construct_crash"}}},
            expected=1)
        atom = self.report_atom(report, "atom/1")
        self.assertEqual(atom["outcome"], "crash")
        self.assertEqual(atom["detail_code"], "crash:construction")
        self.assertIn("simulated construction crash", atom["stderr_tail"])
        self.assertEqual(self.report_atom(report, "atom/2")["outcome"],
                         "legalized")

    def test_crash_records_stderr_tail_and_signal(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus,
            scenario={"atoms": {"atom/1": {"action": "segv"}}},
            expected=1)
        atom = self.report_atom(report, "atom/1")
        self.assertEqual(atom["outcome"], "crash")
        self.assertEqual(atom["detail_code"], "crash:segv")
        self.assertIn("simulated segv", atom["stderr_tail"])
        self.assertEqual(self.report_atom(report, "atom/2")["outcome"],
                         "legalized")
        self.assertEqual(report["counts"]["crash"], 1)

    # -- counts / unbuildable ----------------------------------------------

    def test_counts_conserve_and_first_anomaly(self):
        corpus = make_corpus(7)
        results = [
            make_result(0, "legalized", steps=1),
            make_result(1, "failed", detail_code="libcall"),
            make_result(2, "unbuildable", detail_code="unbuildable:phi",
                        detail="control flow"),
            make_result(3, "construction_mismatch",
                        detail_code="construction_mismatch"),
            make_result(4, "budget", steps=10001, detail_code="budget"),
            make_result(5, "crash", detail_code="crash:segv"),
            make_result(6, "not_run", reason="after_crash"),
        ]
        report = mirrun.build_report(
            corpus, results, self.path("corpus.json"), "sha", "production",
            10000, 120, 2000)
        self.assertEqual(sum(report["counts"].values()), 7)
        self.assertEqual(report["first_anomaly"]["id"], "atom/2")
        # Synthetic C2 M1 semantics: non-family budget/crash are new findings,
        # so A3 is not met even though there is no BUILD_VECTOR coverage gap.
        self.assertEqual(report["a3"]["status"], "not met")
        self.assertEqual(report["a3"]["met"], False)
        self.assertEqual({item["id"] for item in report["new_findings"]},
                         {"atom/4", "atom/5"})
        self.assertEqual(report["a3"]["new_findings"], 2)

    def test_first_anomaly_skips_not_run_placeholders(self):
        corpus = make_corpus(3)
        results = [
            make_result(0, "not_run", reason="only-atom"),
            make_result(1, "budget", steps=10001, detail_code="budget"),
            make_result(2, "not_run", reason="only-atom"),
        ]
        report = mirrun.build_report(
            corpus, results, self.path("corpus.json"), "sha", "production",
            10000, 120, 2000, only_atom="atom/1")
        self.assertEqual(report["first_anomaly"]["id"], "atom/1")
        self.assertEqual(report["counts"]["not_run"], 2)

    def test_unbuildable_reasons_are_preserved(self):
        corpus = make_corpus(2, opcode="G_PHI")
        report, proc = self.run_ok(
            corpus,
            scenario={"atoms": {
                "atom/0": {"action": "normal", "outcome": "unbuildable",
                           "detail_code": "unbuildable:control-flow",
                           "detail": "G_PHI is control flow"},
                "atom/1": {"action": "normal", "outcome": "unbuildable",
                           "detail_code": "unbuildable:control-flow"},
            }, "summary": {"unbuildable": {"G_PHI": "control flow"}}})
        self.assertEqual(self.report_atom(report, "atom/0")["detail"],
                         "G_PHI is control flow")
        self.assertEqual(self.report_atom(report, "atom/1")["detail"],
                         "control flow")
        self.assertEqual(report["unbuildable_reasons"]["G_PHI"],
                         "control flow")
        self.assertEqual(report["counts"]["unbuildable"], 2)

    # -- A3 ----------------------------------------------------------------

    def test_a3_known_build_vector_versus_new_finding(self):
        corpus = make_corpus(5)
        corpus["atoms"][0] = make_atom(
            0, config="C0", rule_set="X86_BUILD_VECTOR",
            opcode="G_BUILD_VECTOR", types=("v3p0:32",),
            expected="MoreElements")
        corpus["atoms"][1] = make_atom(
            1, config="C1", rule_set="X86_BUILD_VECTOR",
            opcode="G_BUILD_VECTOR", types=("v3p0:64",),
            expected="MoreElements")
        corpus["atoms"][2] = make_atom(
            2, config="C2", rule_set="X86_BUILD_VECTOR",
            opcode="G_BUILD_VECTOR", types=("v2s32",),
            expected="MoreElements")
        corpus["atoms"][3] = make_atom(
            3, config="C2", rule_set="X86_MUL", opcode="G_MUL",
            types=("v2s32",), expected="Legal")
        corpus["atoms"][4] = make_atom(
            4, config="C2", rule_set="X86_ADD", opcode="G_ADD",
            types=("v2s32",), expected="Legal")
        results = [make_result(0, "budget", steps=10001,
                               detail_code="budget"),
                   make_result(1, "hang", detail_code="hang:timeout"),
                   make_result(2, "budget", steps=10001,
                               detail_code="budget"),
                   make_result(3, "budget", steps=10001,
                               detail_code="budget"),
                   make_result(4, "crash", detail_code="crash:segv")]
        a3 = mirrun.classify_a3(corpus, results)
        self.assertEqual({item["id"] for item in a3["known_anomalies"]},
                         {"atom/0", "atom/1", "atom/2"})
        self.assertEqual({item["id"] for item in a3["new_findings"]},
                         {"atom/3", "atom/4"})
        self.assertEqual(a3["coverage_gaps"], [])
        # Non-family budget/crash are reportable new findings, so A3 is not
        # met even when every known-family atom reached budget/hang.
        self.assertEqual(a3["a3"]["status"], "not met")
        self.assertEqual(a3["a3"]["met"], False)
        self.assertEqual(a3["a3"]["new_findings"], 2)

    def test_a3_known_family_terminal_is_coverage_gap(self):
        corpus = make_corpus(1)
        corpus["atoms"][0] = make_atom(
            0, rule_set="X86_BUILD_VECTOR", opcode="G_BUILD_VECTOR",
            types=("v2s32",), expected="MoreElements")
        results = [make_result(0, "construction_mismatch",
                               detail_code="construction_mismatch")]
        a3 = mirrun.classify_a3(corpus, results)
        self.assertEqual(a3["a3"]["status"], "not met")
        self.assertEqual([item["id"] for item in a3["coverage_gaps"]],
                         ["atom/0"])
        exempt = mirrun.classify_a3(corpus, results, a2_probe=True)
        self.assertEqual(exempt["a3"]["status"], "met")
        self.assertEqual(exempt["coverage_gaps"], [])

    def test_a3_only_atom_is_exempt(self):
        corpus = make_corpus(3)
        corpus["atoms"][0] = make_atom(
            0, rule_set="X86_BUILD_VECTOR", opcode="G_BUILD_VECTOR",
            types=("v2s32",), expected="MoreElements")
        corpus["atoms"][1] = make_atom(
            1, rule_set="X86_BUILD_VECTOR", opcode="G_BUILD_VECTOR",
            types=("v2s16",), expected="MoreElements")
        results = [
            make_result(0, "not_run", reason="only-atom"),
            make_result(1, "budget", steps=10001, detail_code="budget"),
            make_result(2, "not_run", reason="only-atom"),
        ]
        a3 = mirrun.classify_a3(corpus, results, only_atom="atom/1")
        self.assertTrue(a3["a3"]["exempt"])
        self.assertEqual(a3["a3"]["status"], "met")
        self.assertEqual(a3["coverage_gaps"], [])
        self.assertEqual([item["id"] for item in a3["known_anomalies"]],
                         ["atom/1"])

    def test_a3_construction_crash_is_coverage_gap(self):
        corpus = make_corpus(1)
        corpus["atoms"][0] = make_atom(
            0, rule_set="X86_BUILD_VECTOR", opcode="G_BUILD_VECTOR",
            types=("v2s32",), expected="MoreElements")
        results = [make_result(0, "crash",
                               detail_code="crash:construction")]
        a3 = mirrun.classify_a3(corpus, results)
        self.assertEqual(a3["a3"]["status"], "not met")
        self.assertEqual(a3["known_anomalies"], [])

    # -- exit contract / schema --------------------------------------------

    def test_exit_code_missing_binary_is_two(self):
        corpus_path = self.write_corpus(make_corpus(1))
        out = self.path("report.json")
        proc = self.run_cli("--corpus", corpus_path,
                            "--driver", self.path("does-not-exist"),
                            "--out", out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("infrastructure error", proc.stderr)
        self.assertFalse(os.path.exists(out))

    def test_exit_code_terminal_is_zero(self):
        corpus = make_corpus(1)
        report, proc = self.run_ok(
            corpus,
            scenario={"atoms": {"atom/0": {"action": "normal",
                                           "outcome": "unbuildable",
                                           "detail": "why"}}})
        self.assertEqual(proc.returncode, 0)

    def test_unexplained_not_run_is_exit_two(self):
        corpus = make_corpus(1)
        report = mirrun.build_report(
            corpus, [make_result(0, "not_run", reason="mystery")],
            self.path("corpus.json"), "sha", "production", 10000, 120, 2000)
        self.assertEqual(mirrun.exit_code(report), 2)

    def test_timeout_before_any_marker_is_not_run_not_crash(self):
        corpus = make_corpus(2)
        report, proc = self.run_ok(
            corpus, "--timeout", "1",
            scenario={"sleep_before_start": 30}, expected=2)
        self.assertEqual(report["counts"]["hang"], 0)
        self.assertEqual(report["counts"]["crash"], 0)
        self.assertEqual({atom["reason"] for atom in report["atoms"]},
                         {"timeout_before_start"})
        self.assertEqual(mirrun.exit_code(report), 2)

    def test_pre_start_timeout_retries_with_grace(self):
        corpus = make_corpus(2)
        report, proc = self.run_ok(
            corpus, "--timeout", "1",
            scenario={"sleep_before_start": 2}, expected=0)
        self.assertEqual(report["counts"]["legalized"], 2)
        self.assertIn("retrying shard after a pre-start timeout",
                      "\n".join(report["infra_warnings"]))

    def test_timeout_between_atoms_is_not_run_not_crash(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus, "--timeout", "1",
            scenario={"atoms": {"atom/1": {"action": "stall_before_start"}}},
            expected=2)
        self.assertEqual(report["counts"]["crash"], 0)
        self.assertEqual(report["counts"]["hang"], 0)
        self.assertEqual(self.report_atom(report, "atom/0")["outcome"],
                         "legalized")
        self.assertEqual(self.report_atom(report, "atom/1")["reason"],
                         "timeout_before_start")
        # The retry starts at atom/1, observes no marker at all, and reports
        # the whole remaining shard as a pre-start timeout.
        self.assertEqual(self.report_atom(report, "atom/2")["reason"],
                         "timeout_before_start")

    def test_termination_between_atoms_is_not_run_not_crash(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus, "--timeout", "5",
            scenario={"atoms": {"atom/1": {"action": "abort_before_start"}}},
            expected=2)
        self.assertEqual(report["counts"]["crash"], 0)
        self.assertEqual(report["counts"]["hang"], 0)
        self.assertEqual(self.report_atom(report, "atom/0")["outcome"],
                         "legalized")
        self.assertEqual(self.report_atom(report, "atom/1")["reason"],
                         "no_atom_observed")
        self.assertEqual(self.report_atom(report, "atom/2")["reason"],
                         "no_atom_observed")

    def test_classify_timeout_between_atoms_is_not_run(self):
        atoms = [(0, {"id": "atom/0", "index": 0, "opcode": "G_ADD"}),
                 (1, {"id": "atom/1", "index": 1, "opcode": "G_ADD"})]
        events = [
            {"event": "start", "id": "atom/0", "index": 0},
            {"event": "constructed", "id": "atom/0", "index": 0},
            {"event": "legalize", "id": "atom/0", "index": 0},
            {"event": "outcome", "id": "atom/0", "index": 0,
             "opcode": "G_ADD", "outcome": "legalized", "steps": 1,
             "detail_code": "ok", "detail": "legalizer terminated"},
        ]
        shard = mirrun.classify_shard(events, atoms, timed_out=True)
        self.assertEqual(shard["results"][0]["outcome"], "legalized")
        self.assertEqual(shard["results"][1]["outcome"], "not_run")
        self.assertEqual(shard["results"][1]["reason"],
                         "timeout_before_start")
        self.assertTrue(any("start marker" in warning
                            for warning in shard["warnings"]))

    def test_classify_termination_between_atoms_is_not_run(self):
        atoms = [(0, {"id": "atom/0", "index": 0, "opcode": "G_ADD"}),
                 (1, {"id": "atom/1", "index": 1, "opcode": "G_ADD"})]
        events = [
            {"event": "start", "id": "atom/0", "index": 0},
            {"event": "constructed", "id": "atom/0", "index": 0},
            {"event": "legalize", "id": "atom/0", "index": 0},
            {"event": "outcome", "id": "atom/0", "index": 0,
             "opcode": "G_ADD", "outcome": "legalized", "steps": 1,
             "detail_code": "ok", "detail": "legalizer terminated"},
        ]
        shard = mirrun.classify_shard(events, atoms,
                                      returncode=-signal.SIGSEGV)
        self.assertEqual(shard["results"][1]["outcome"], "not_run")
        self.assertEqual(shard["results"][1]["reason"],
                         "unattributed_termination")
        self.assertEqual(shard["results"][1]["detail_code"], "not_run")

    def test_pre_legalize_timeout_is_exit_two(self):
        corpus = make_corpus(2)
        report, proc = self.run_ok(
            corpus, "--timeout", "1",
            scenario={"atoms": {"atom/0": {"action": "slow_construct"}}},
            expected=2)
        self.assertEqual(self.report_atom(report, "atom/0")["reason"],
                         "slow_or_stuck_before_legalizer")

    def test_only_atom_anomaly_is_exit_one(self):
        corpus = make_corpus(3)
        report, proc = self.run_ok(
            corpus, "--only-atom", "atom/1",
            scenario={"atoms": {"atom/1": {"action": "budget"}}},
            expected=1)
        self.assertEqual(report["counts"]["budget"], 1)

    def test_report_schema(self):
        corpus = make_corpus(2)
        report, proc = self.run_ok(
            corpus, "--rules", "production", "--shard-size", "2000",
            "--timeout", "120", "--max-steps", "10000")
        self.assertEqual(report["schema"], "llvm-x86-legalizer-mir-run")
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["libcalls"], "disabled")
        self.assertEqual(report["rules"], "production")
        self.assertEqual(report["budget"],
                         {"max_steps": 10000, "timeout_s": 120,
                          "shard_size": 2000})
        with open(self.path("corpus.json"), "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(report["corpus"]["sha256"], digest)
        self.assertEqual(report["corpus"]["atoms"], 2)
        self.assertEqual(report["corpus"]["config"], "C2")
        self.assertEqual(set(report["counts"]), set(mirrun.COUNT_KEYS))
        self.assertEqual(sum(report["counts"].values()), 2)
        for key in ("atoms", "anomalies", "new_findings", "known_anomalies",
                    "coverage_gaps", "first_anomaly", "a3"):
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
