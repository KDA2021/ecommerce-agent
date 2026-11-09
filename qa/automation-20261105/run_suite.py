"""Explicitly invoked runner; no collection or test execution on import."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["offline", "mysql"], default="offline")
    parser.add_argument("--include-web", action="store_true")
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="Reuse unchanged successful Python evidence; rerun browser phases",
    )
    args = parser.parse_args()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(HERE)])
    env["PWTEST_CACHE_DIR"] = str(HERE / "artifacts" / "playwright-cache")
    temporary = HERE / "artifacts" / "temp"
    temporary.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = env["TMP"] = str(temporary)
    for name in ["LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"]:
        env[name] = ""
    if args.suite == "offline":
        env["TEST_MYSQL_URL"] = ""
    else:
        # Never accept a user database URL: the only allowed endpoint is QA port.
        env["TEST_MYSQL_URL"] = (
            "mysql+asyncmy://root:qa-only-20261105@127.0.0.1:33067/qa_automation?charset=utf8mb4"
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = HERE / "artifacts" / f"{stamp}-{args.suite}"
    out.mkdir(parents=True)
    config = json.loads((HERE / "suites.json").read_text())[args.suite]
    xml = out / "pytest.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(HERE / "pytest.ini"),
        "-p",
        "local_guard",
        *config["paths"],
        "-k",
        config["selection"],
        "-q",
        "--durations=5",
        f"--junitxml={xml}",
    ]
    print(f"Running {args.suite}; reports: {out}", flush=True)
    reused = None
    if args.web_only:
        assert args.suite == "offline" and args.include_web
        pointer = json.loads((HERE / "artifacts/latest-offline.json").read_text(encoding="utf-8"))
        previous = Path(pointer["report"])
        evidence = json.loads(previous.read_text(encoding="utf-8"))
        assert evidence["pytest_exit_code"] == 0
        assert evidence["pytest"]["collected_results"] > 0
        assert not any(evidence["pytest"][k] for k in ["errors", "failures", "skipped"])
        for name, digest in evidence["sha256"].items():
            if name.endswith("run_suite.py") or name.endswith(".mjs"):
                continue
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
        shutil.copy2(previous.parent / "pytest.xml", xml)
        shutil.copy2(previous.parent / "pytest.log", out / "pytest.log")
        reused = str(previous)
        code = 0
        print(
            "Python code and evidence unchanged; rerunning only failed browser phases", flush=True
        )
    else:
        # Stream output and keep it verbatim; never swallow a failed suite.
        with (out / "pytest.log").open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in proc.stdout:
                print(line, end="", flush=True)
                log.write(line)
            code = proc.wait()
    stats = {}
    if xml.exists():
        tree = ET.parse(xml).getroot()
        cases = tree.findall(".//testcase")
        stats = {
            "collected_results": len(cases),
            "failures": len(tree.findall(".//failure")),
            "errors": len(tree.findall(".//error")),
            "skipped": len(tree.findall(".//skipped")),
        }
    valid = (
        code == 0
        and stats.get("collected_results", 0) > 0
        and not any(stats.get(k, 0) for k in ["failures", "errors", "skipped"])
    )
    web_results = []
    if args.include_web:
        node = env.get("QA_NODE") or shutil.which("node")
        cli = env.get("QA_PLAYWRIGHT_CLI")
        if not node or not cli:
            raise RuntimeError("Set QA_NODE and QA_PLAYWRIGHT_CLI to installed runtime paths")
        for name, folder, report in [
            ("mock-ui", HERE, "mock-browser.json"),
            ("live-service-ui", HERE, "browser.json"),
        ]:
            print(f"Running {name}", flush=True)
            completed = subprocess.run(
                [
                    node,
                    cli,
                    "test",
                    "--config="
                    + (
                        "mock-playwright.config.mjs"
                        if name == "mock-ui"
                        else "playwright.config.mjs"
                    ),
                ],
                cwd=folder,
                env=env,
            )
            source = folder / "artifacts" / report
            data = json.loads(source.read_text(encoding="utf-8-sig")) if source.exists() else {}
            s = data.get("stats", {})
            ok = (
                completed.returncode == 0
                and s.get("expected", 0) > 0
                and not any(s.get(k, 0) for k in ["unexpected", "flaky", "skipped"])
            )
            web_results.append(
                {"suite": name, "exit_code": completed.returncode, "stats": s, "passed_gate": ok}
            )
            dest = out / name
            if (folder / "artifacts").exists():
                if name == "mock-ui":
                    dest.mkdir()
                    shutil.copy2(source, dest / report) if source.exists() else None
                    for sub in ["mock-browser-report", "mock-browser-results"]:
                        if (folder / "artifacts" / sub).exists():
                            shutil.copytree(folder / "artifacts" / sub, dest / sub)
                else:
                    dest.mkdir()
                    if source.exists():
                        shutil.copy2(source, dest / report)
                    for sub in ["browser-report", "browser-results"]:
                        if (HERE / "artifacts" / sub).exists():
                            shutil.copytree(HERE / "artifacts" / sub, dest / sub)
            valid = valid and ok
    files = (
        list(HERE.glob("*.py"))
        + list((HERE / "web").glob("*.mjs"))
        + [HERE / "suites.json", HERE / "playwright.config.mjs", HERE / "vite.config.mjs"]
        + [ROOT / "app/infrastructure/eventbroker.py", ROOT / "app/presentation/routers/events.py"]
    )
    files += [
        ROOT / p.split("::")[0] for p in config["paths"] if (ROOT / p.split("::")[0]).is_file()
    ]
    result = {
        "created_utc": stamp,
        "suite": args.suite,
        "boundary": config["boundary"],
        "pytest_exit_code": code,
        "pytest_reused_from": reused,
        "pytest": stats,
        "web": web_results,
        "passed_gate": valid,
        "llm_calls": 0,
        "sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
        },
    }
    (out / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (HERE / "artifacts" / f"latest-{args.suite}.json").write_text(
        json.dumps({"report": str(out / "summary.json")}, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
