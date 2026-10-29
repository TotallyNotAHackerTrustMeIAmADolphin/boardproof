# SPICE fixtures recorded from a real ngspice

Recorded 2026-10-28 with ngspice-47 (official Windows build `ngspice-47_64.7z` from
ngspice.sourceforge.io, `ngspice_con.exe -b`), on the netlists beside them (written by hand in the shape
kicad-cli's SPICE export gives an RC low-pass: V1 -> R1 1k -> C1 100n -> 0).

- `rc_lowpass.cir`, `rc_pulse.cir`: the netlists (`rc_pulse.cir` has a 0-to-1 V step source).
- `rc_lowpass_op.wrdata`, `rc_lowpass_ac.wrdata`, `rc_pulse_tran.wrdata`: the raw `wrdata` file for
  `v(Net-_R1-Pad1_) v(Net-_C1-Pad1_)` after `op`, `ac dec 2 10 10k`, `tran 100us 500us`.
- `rc_lowpass_missing_probe.stdout`: stdout for a probe `v(nosuch)`.

`tests/test_simulation_toolset.py` parses them, and its live tests re-run the same circuits and compare.
Re-record all of them together when the pinned version changes (`tests/_ngspice.py`, `PINNED_VERSION`).
