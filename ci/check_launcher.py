"""Render scripts/user_data.sh.tftpl the way Terraform's templatefile() does,
then check that the launcher stays POSIX (Lightsail runs it under /bin/sh).

Usage: python3 ci/check_launcher.py <output-file>
"""
import re
import sys
from pathlib import Path

tpl = Path("scripts/user_data.sh.tftpl").read_text()
body = Path("scripts/wg-setup.sh").read_text()

# The template must use exactly one interpolation, ${wg_setup}, and no directives,
# so a plain substitution renders it exactly like templatefile().
interps = re.findall(r"(?<!\$)\$\{[^}]*\}|(?<!%)%\{[^}]*\}", tpl)
if interps != ["${wg_setup}"]:
    sys.exit(f"unexpected template sequences: {interps}")

errors = []
for n, line in enumerate(tpl.splitlines(), 1):
    code = line.split("#", 1)[0]  # comments may mention pipefail
    if "pipefail" in code:
        errors.append(f"line {n}: pipefail is not POSIX")
    if "[[" in code:
        errors.append(f"line {n}: [[ is bash-only")
    if re.match(r"\s*#!.*bash", line) and n == 1:
        errors.append("line 1: Lightsail ignores a bash shebang")
if errors:
    sys.exit("\n".join(errors))

Path(sys.argv[1]).write_text(tpl.replace("${wg_setup}", body))
print("launcher rendered and passes POSIX checks")
