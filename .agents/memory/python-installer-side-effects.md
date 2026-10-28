---
name: Python installer side effects
description: Unexpected system dependency changes from the managed Python installer on NixOS.
---

Inspect system dependency changes after managed Python package installation.

**Why:** Installing lxml, pytest, and httpx automatically added `xcodebuild` and
`zlib` to the Nix configuration, even though this Linux app used a working lxml
wheel and did not need these additions. `xcodebuild` is inappropriate for NixOS.

**How to apply:** Review the configuration diff after installing Python packages.
Remove unsupported, unnecessary generated additions using the validated
configuration replacement tool; do not change the application's existing runtime.
