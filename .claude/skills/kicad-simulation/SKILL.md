---
name: kicad-simulation
description: Use when simulating a circuit with SPICE -- e.g. "simulate this", "what is the cutoff frequency", "check the operating point", "run a transient", "plot the output", "export a SPICE netlist". Wraps this project's own `simulation` MCP toolset (export_spice_netlist, run_spice_simulation).
---

# Simulation workflow

The `simulation` toolset is small and says exactly what it measured. Read the result as a list of numbers, not as a verdict on the circuit.

1. **Export, then run.** `export_spice_netlist(sch_path, output_path)` writes the netlist with kicad-cli. `run_spice_simulation(netlist_path, analysis)` runs the real ngspice on it. Probe names are the node names **as they appear in the exported netlist** (kicad-cli names an unnamed net like `Net-_C1-Pad1_`); read the netlist first. A probe name ngspice does not know comes back as `status: incomplete` with the name in `reason`.
2. **One call, one analysis, any number of probes.** `analysis` is `{"type": "op"|"tran"|"ac", "probe": [...]}` plus `step`/`stop` (tran) or `sweep`/`points`/`fstart`/`fstop` (ac). Every probe returns its own series; there is no "last probe wins".
3. **Read the result shape.** `status`, `analysis`, `probes` (call order, duplicates dropped), `points`, `x` (`{name, unit, values}`: `time` in s, `frequency` in Hz, or `null` for `op`) and `series` (`{probe: {unit, quantity, values}}`). `unit` is `V`. `quantity` is `voltage` for `op` and `tran`, `magnitude` (linear, not dB) for `ac`. Every series has the same length as `x`.
4. **AC cutoff.** For `ac`, each series also carries `cutoff_frequency_hz`: where that node's magnitude first falls to 1/sqrt(2) of its own maximum, or `null` when it never does (a source node never does). With exactly one probe the same number is also at the top level. If no probe has such a crossing the status is `incomplete`: the data is returned, but no -3 dB point exists in the swept range. That is the right answer for anything that is not a low-pass filter; read `series` instead.
5. **What is not returned.** Branch or device currents, AC phase, dB, `.meas` results, `dc` or `.step` sweeps, several analyses in one call, plots, and a dump of every node. Do not work around the gap by inventing numbers; say it is not measured. A voltage can stand in for a current only through a sense resistor you add to the circuit on purpose.
6. **A converged run is not a working circuit.** `status: ok` means ngspice produced numbers for the probes you named, using the models the netlist contains. It does not mean the circuit meets a requirement, that a part model is the real part, or that unprobed nodes are fine. Compare the numbers with a hand calculation or a datasheet figure before saying anything works, and name what you compared.
7. **ngspice must be installed.** `NGSPICE_PATH` (on Windows `ngspice_con.exe`, never the GUI `ngspice.exe`, which hangs in batch mode), then PATH. Missing: `status: incomplete`, never a made-up result. The project's tests are pinned to ngspice-47.
