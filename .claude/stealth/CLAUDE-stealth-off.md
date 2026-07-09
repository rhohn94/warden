<!-- STEALTH_SECTION:start -->
Stealth Mode is **off** (`stealth-mode.value: "off"`). Grimoire operates
normally — its files, branches, and commit metadata are handled as usual. To
make Grimoire leave **zero AI/agent fingerprints** in source control, activate
it via the **`grm-stealth-mode-switch`** skill. Activation discloses one trade-off
you must acknowledge: the Grimoire context becomes **ephemeral** (local-only,
never committed), so deleting the local clone loses it. Design:
`docs/design/stealth-mode-design.md`.
<!-- STEALTH_SECTION:end -->
