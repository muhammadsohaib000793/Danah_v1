"""Build web/index.html — the v11 prototype wired to the real backend.

Kept as a build step rather than a hand-edited copy so the original prototype stays the
single source of truth for the UI, and every change made to it on the way to production is
listed here in one place, reviewable.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "DANAH_Strategic_Intelligence_Platform_v11.html"
OUT = ROOT / "web" / "index.html"

INJECT = """
<!-- ===== DANAH backend integration =====
     Loads AFTER the prototype's script and redefines the four functions that were
     simulations: signIn (dummy SSO -> argon2 + JWT), askDanah (keyword matching -> a real
     model with real citations), runPipeline (14 strings on a timer -> a real orchestrator
     run, polled), approveDecision (array splice -> the real human approval gate).
     The screens are untouched. Only the source of truth changes. -->
<script src="danah-api.js"></script>
"""

# (pattern, replacement, why)
PATCHES: list[tuple[str, str, str]] = [
    (
        r"\+', Fatma'",
        "+', '+((typeof SESSION!=='undefined'&&SESSION&&SESSION.name)?SESSION.name.split(' ')[0]:'')",
        "greet the user who actually signed in, not a name hard-coded into the prototype",
    ),
]


def main() -> int:
    html = SRC.read_text(encoding="utf-8", errors="replace")

    for pattern, repl, why in PATCHES:
        html, n = re.subn(pattern, repl, html)
        if n == 0:
            print(f"  !! patch did not apply ({why}) — pattern: {pattern}")
            return 1
        print(f"  [ok] {why}  (x{n})")

    if "</body>" not in html:
        print("  !! no </body> in the prototype")
        return 1
    i = html.rindex("</body>")
    html = html[:i] + INJECT + html[i:]
    print("  [ok] integration script injected before </body>")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"\nweb/index.html written ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
