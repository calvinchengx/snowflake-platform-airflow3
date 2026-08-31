"""Every guard script this repository ships is reachable from CI.

WHY THIS EXISTS. `fabric-emulator` found three of its eleven `make check`
scripts were never invoked by `ci.yml`: they ran only on the machine of
whoever happened to type `make check`. All three passed the whole time, which
is why it survived -- **a check that passes is indistinguishable from a check
that is running**, and the only way to tell them apart is to ask which one CI
invokes. Its own guard then caught the same omission twice more, in the two
commits written to close that gap.

WHAT THIS DOES DIFFERENTLY, and it matters outside fabric-emulator. There a
script is reachable only if `ci.yml` names it literally. Here a script is
usually reached through `make`: `check_product_pin.py` lives in the `pin`
target, nothing names it in a workflow, and it runs on every acceptance run
because `up: doctor sources pin manifest` and the workflow calls `make up`. A
check that only looked for the filename in CI would call that unrun and be
wrong. So reachability is resolved the way make resolves it: a target CI
invokes, plus everything that target depends on, transitively.

Two parsing details, both learned by getting them wrong:
  * a `#` comment inside a recipe does NOT end the target, and treating it as
    a terminator silently drops every line after it;
  * prerequisites are part of reachability, not decoration.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
MAKEFILE = REPO / "Makefile"
WORKFLOWS = REPO / ".github" / "workflows"

# Scripts deliberately unreachable from CI belong here WITH A REASON, so an
# exemption is a decision someone wrote down rather than an omission nobody saw.
LOCAL_ONLY: dict[str, str] = {}

TARGET = re.compile(r"^([A-Za-z0-9_.%-]+)\s*:(?!=)([^=]*)$")


def _makefile() -> dict[str, tuple[set[str], str]]:
    """target -> (prerequisites, recipe text)."""
    out: dict[str, tuple[set[str], list[str]]] = {}
    current = None
    for line in MAKEFILE.read_text(encoding="utf-8").split("\n"):
        if line.startswith("\t"):
            if current:
                out[current][1].append(line)
            continue
        m = TARGET.match(line)
        if m and not line.startswith("."):
            name = m.group(1)
            prereqs = {p for p in m.group(2).split("##")[0].split() if p}
            prev = out.get(name)
            out[name] = (prereqs | (prev[0] if prev else set()),
                         prev[1] if prev else [])
            current = name
        elif not line.strip():
            continue          # a blank line inside a recipe is still the recipe
        elif line.lstrip().startswith("#"):
            continue          # NOR does a comment end it -- see the docstring
        else:
            current = None
    return {k: (p, "\n".join(b)) for k, (p, b) in out.items()}


def _ci_text() -> str:
    if not WORKFLOWS.is_dir():
        return ""
    return "".join(p.read_text(encoding="utf-8")
                   for p in sorted(WORKFLOWS.glob("*.yml")))


def _reachable_targets(mk, ci: str) -> set[str]:
    """Targets CI invokes, plus everything they depend on, transitively."""
    seen, stack = set(), [t for t in mk
                          if re.search(rf"make\s+(?:-[^\s]+\s+)*{re.escape(t)}\b", ci)]
    while stack:
        t = stack.pop()
        if t in seen or t not in mk:
            continue
        seen.add(t)
        stack.extend(mk[t][0])
    return seen


def _guard_scripts() -> set[str]:
    d = REPO / "scripts"
    return {p.name for p in d.glob("check_*.py")} if d.is_dir() else set()


def test_the_makefile_is_parsed_at_all():
    """A parser that matches nothing turns this file into a vacuous pass --
    the exact defect it exists to prevent, one level up."""
    mk = _makefile()
    assert len(mk) >= 5, f"parsed {len(mk)} targets from the Makefile; the parser is broken"
    assert any(body for _, body in mk.values()), "every target parsed with an empty recipe"


def test_ci_invokes_at_least_one_make_target():
    """Likewise: if nothing matches, every script looks unreachable and this
    test would fail for the wrong reason -- or pass because the set is empty."""
    mk = _makefile()
    assert _reachable_targets(mk, _ci_text()), "no `make <target>` found in any workflow"


def test_every_guard_script_is_reachable_from_ci():
    mk, ci = _makefile(), _ci_text()
    reachable = _reachable_targets(mk, ci)
    run_by_make = {s for t in reachable for s in re.findall(r"scripts/([a-z_0-9]+\.py)", mk[t][1])}
    run_by_ci = set(re.findall(r"scripts/([a-z_0-9]+\.py)", ci))
    missing = sorted(_guard_scripts() - run_by_make - run_by_ci - set(LOCAL_ONLY))
    assert not missing, (
        "these guard scripts are enforced only on a developer's machine -- no "
        f"workflow runs them and no make target CI invokes reaches them: {missing}\n"
        "Wire them into CI, or record an exemption with its reason in LOCAL_ONLY."
    )
