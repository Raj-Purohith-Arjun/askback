"""The package boundary: `askback` must stand without `askback_sim`.

Two gates, because each catches what the other cannot.

  static   No module under the framework packages contains an import of the
           simulator - at module level, inside a function, or under
           TYPE_CHECKING. Reads the source, so it names the file and the line.
           There are no exceptions and there is no allowlist: an exception here
           is how a boundary decays.

  dynamic  A fresh interpreter in which `askback_sim` refuses to import can still
           import every framework module and the CLI, and run an evaluation.
           This is the one that proves the claim; the static gate cannot see an
           import reached through a package `__init__`, a registry filled as a
           side effect, or `importlib`.

`askback/cli.py` and `askback/api/` are outside the static gate on purpose:
they are the composition roots, the only places allowed to know the simulator
exists, and they import it lazily. The dynamic gate still covers the CLI.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

FRAMEWORK = "askback"
SIMULATOR = "askback_sim"
SRC = Path(__file__).parent.parent / "src"

#: The framework packages held to the rule.
PACKAGES = ("core", "datasets", "adapters", "evaluation", "reporting", "runner", "providers")


def _imported_modules(path: Path, root: Path = SRC) -> list[tuple[int, str]]:
    """Every absolute module name `path` imports, with its line. Relative ones resolved."""
    relative = path.relative_to(root).with_suffix("")
    # The package a relative import resolves against: the module's parent.
    package = list(relative.parts[:-1])

    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            # `from .. import engine` names the submodule in the alias, not in
            # the module, so each alias is checked as a candidate module too.
            found.append((node.lineno, module))
            found.extend((node.lineno, f"{module}.{alias.name}") for alias in node.names)
    return found


def _is_simulator(module: str) -> bool:
    return module.split(".")[0] == SIMULATOR


def test_the_gate_walks_real_files_and_can_see_an_offender(tmp_path: Path) -> None:
    """Guards the gate: an import scan that finds nothing must be able to find something."""
    framework_files = [f for name in PACKAGES for f in (SRC / FRAMEWORK / name).rglob("*.py")]
    assert len(framework_files) >= 30, "the scan is not looking at the framework packages"

    # The simulator's own engine imports the simulator; the scan must say so.
    seen = {module for _, module in _imported_modules(SRC / SIMULATOR / "engine.py")}
    assert any(_is_simulator(m) for m in seen)
    # Each form an import can take: absolute, bare, and inside a function.
    probe = tmp_path / FRAMEWORK / "evaluation" / "probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        "from askback_sim.llm.base import X\nimport askback_sim\n"
        "def f():\n    from askback_sim import engine\n",
        encoding="utf-8",
    )
    offenders = sorted({m for _, m in _imported_modules(probe, tmp_path) if _is_simulator(m)})
    assert offenders == [
        "askback_sim",
        "askback_sim.engine",
        "askback_sim.llm.base",
        "askback_sim.llm.base.X",
    ]


def test_no_framework_module_imports_the_simulator() -> None:
    offenders: list[str] = []
    for name in PACKAGES:
        for path in sorted((SRC / FRAMEWORK / name).rglob("*.py")):
            seen: set[tuple[int, str]] = set()
            for line, module in _imported_modules(path):
                if not _is_simulator(module):
                    continue
                # Report the import once, by its module, not once per alias.
                target = ".".join(module.split(".")[:2])
                if (line, target) not in seen:
                    seen.add((line, target))
                    offenders.append(
                        f"  {path.relative_to(SRC.parent).as_posix()}:{line} imports {target}"
                    )

    assert not offenders, (
        f"{len(offenders)} import(s) cross from the framework into the simulator. The "
        "framework must import with the simulator absent; pass what is needed in, or "
        "move it to the framework side:\n" + "\n".join(offenders)
    )


# What the blocked interpreter runs. Written to exercise the framework, not just
# import it: "imports and runs with zero simulator code present".
_WITHOUT_THE_SIMULATOR = r"""
import importlib, importlib.abc, pkgutil, sys

FRAMEWORK, SIMULATOR, PACKAGES = {framework!r}, {simulator!r}, {packages!r}


class Absent(importlib.abc.MetaPathFinder):
    # What an uninstalled package looks like to the import system.
    def find_spec(self, fullname, path=None, target=None):
        if fullname == SIMULATOR or fullname.startswith(SIMULATOR + "."):
            raise ModuleNotFoundError(f"No module named {{fullname!r}}", name=fullname)
        return None


sys.meta_path.insert(0, Absent())

imported, without_extra = [], []
for name in PACKAGES:
    root = importlib.import_module(f"{{FRAMEWORK}}.{{name}}")
    names = [root.__name__] + [
        m.name for m in pkgutil.walk_packages(root.__path__, root.__name__ + ".")
    ]
    for module in names:
        try:
            importlib.import_module(module)
            imported.append(module)
        except ModuleNotFoundError as exc:
            # A third-party extra that is not installed (httpx, openpyxl) is not
            # the simulator; anything of ours that is missing is a failure.
            if (exc.name or "").split(".")[0] in (FRAMEWORK, SIMULATOR):
                raise
            without_extra.append(f"{{module}} (needs {{exc.name}})")

cli = importlib.import_module(f"{{FRAMEWORK}}.cli")
cli.build_parser()

# Run an evaluation end to end: scripted agent, default evaluators, real gates.
from {framework}.adapters.scripted import ScriptedAdapter
from {framework}.core.contracts import ChatResponse
from {framework}.datasets.schema import Expectations, TestCase, Turn
from {framework}.evaluation.engine import EvaluationEngine
from {framework}.evaluation.gates import evaluate_gates
from {framework}.runner import TestRunner

adapter = ScriptedAdapter(
    replies={{}}, default=ChatResponse(reply="You get 24 days of leave.", conversation_id="s")
)
engine = EvaluationEngine.from_config({{}})
report = TestRunner(adapter, engine, workers=1).run(
    [TestCase(id="c1", turns=[Turn(user="leave?", expect=Expectations(required_facts=["24 days"]))])]
)
# Nothing declares side-effecting tools without the simulator, so the framework must
# say it cannot judge them (invariant 8) instead of passing the case.
case = report.cases[0]
assert not case.error, case
assert [c.name for c in case.failed_checks] == ["tool_side_effects"], case
assert report.context["judge"] == "heuristic", report.context
evaluate_gates(report, {{}})

# The default run, with no --config, grades the built-in demo and must work with the
# simulator absent: it is what a plain install does on its first command.
import contextlib, io, tempfile

with tempfile.TemporaryDirectory() as reports, contextlib.redirect_stdout(io.StringIO()):
    code = cli.main(["eval", "--quiet", "--format", "json", "--reports-dir", reports])
assert code == cli.EXIT_OK, f"the default demo run failed without the simulator: exit {{code}}"

# The commands that ARE the simulator say so, and exit as an operator error.
HR = "examples/hr/config.yaml"
commands = [["info"], ["ask", "hello"], ["chat"], ["eval", "--quiet"]]
try:
    import uvicorn  # noqa: F401 - without the server extra, `serve` stops earlier
    commands.append(["serve"])
except ImportError:
    pass
for command in commands:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = cli.main(["--config", HR, *command])
    assert code == cli.EXIT_ERROR, (command, code, err.getvalue())
    assert "source checkout" in err.getvalue(), (command, err.getvalue())

loaded = sorted(m for m in sys.modules if m == SIMULATOR or m.startswith(SIMULATOR + "."))
assert not loaded, f"simulator modules were loaded: {{loaded}}"
print(f"imported={{len(imported)}}")
for line in without_extra:
    print(f"skipped={{line}}")
"""


def test_the_framework_imports_and_runs_with_the_simulator_absent() -> None:
    script = _WITHOUT_THE_SIMULATOR.format(
        framework=FRAMEWORK, simulator=SIMULATOR, packages=PACKAGES
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    # The judge reads JUDGE_-prefixed variables; none may leak in from a dev shell.
    env = {k: v for k, v in env.items() if not k.startswith("JUDGE_")}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=SRC.parent,
        env=env,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, (
        "the framework does not import or run with the simulator blocked:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    imported = int(result.stdout.split("imported=")[1].split()[0])
    assert imported >= 30, f"only {imported} framework modules were imported:\n{result.stdout}"
