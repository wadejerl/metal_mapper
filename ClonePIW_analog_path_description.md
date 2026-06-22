# ClonePI-W Analog Signal Path Description
## For use in microcontroller porting sessions

---

## Overview

The ClonePI-W is a pulse-induction (PI) metal detector built around an ATmega8 microcontroller, a TL074 quad op-amp, and an ADG444 quad CMOS analog switch. The analog path covers coil driving, receive signal amplification, DC servo stabilization, and per-pulse integration. The ADC result feeds the ATmega8 firmware which drives LEDs and audio output. This document covers everything up to the ADC input.

---

## Components Referenced

- **VT1**: IRF740 (or IRF730) N-channel MOSFET — coil TX switch
- **L1**: 300–400µH search coil
- **VD1, VD2**: 1N4148 — flyback clamp diodes
- **R1, R3**: 390Ω each — coil damping resistors
- **R2**: 20Ω — VT1 gate resistor
- **R4**: 10K — VT1 gate pull-down
- **U4 (ADG444)**: Quad CMOS analog switch — four sections U4A, U4B, U4C, U4D
- **U3 (TL074)**: Quad op-amp — four sections U3A, U3B, U3C, U3D
- **R8**: 1K — U3D input resistor
- **R11**: 10K — U3D feedback resistor
- **R13**: 1K — U3A lower gain resistor
- **R15**: 56K (or 68K variant) — U3A feedback resistor
- **R14**: 2M4 adjustable (2M0–2M7) — U3C DC servo time constant / sensitivity trim
- **C3**: 0.1µF — U3C integrating capacitor
- **R17**: 3K — U3B integrator input resistor
- **C5**: 2200µF (or 1500µF variant) — U3B integrating capacitor
- **U5 (TL431)**: Precision voltage reference — generates Uref (~4.5V on 9V supply)
- **R19, R20**: 10K / 12K — Uref voltage divider

---

## Functional Block Descriptions

### 1. TX Stage

The ATmega8 asserts a digital HIGH on the U4A control pin, closing U4A and driving the gate of VT1 (IRF740) through R2 (20Ω). R4 (10K) pulls the gate low when U4A is open, ensuring VT1 stays off between pulses. While VT1 conducts, current ramps linearly through L1. When the ATmega8 de-asserts U4A, VT1 switches off abruptly, producing a large inductive flyback spike. VD1 and VD2 (1N4148) clamp this spike. R1 and R3 (390Ω each) damp the coil ring-down, controlling how quickly the residual oscillation settles.

### 2. Blanking Period

Immediately after TX cutoff, the coil ring-down signal is still too large for the receive chain. During this blanking period, U4B remains asserted (shorting R15, holding U3A at unity gain) and U4D remains de-asserted (RX path disconnected). The blanking period must be long enough for the coil signal to decay to a level within the linear range of the amplifier chain.

### 3. First Gain Stage — U3D (−10×)

U3D is an inverting amplifier:
- Input resistor R8 = 1K (inverting input)
- Feedback resistor R11 = 10K
- Gain = −R11/R8 = −10×
- R9 (10K) on the non-inverting input balances bias currents

U3D isolates the receive signal source and provides the first stage of amplification.

### 4. Second Gain Stage — U3A, gain-switched by U4B

U3A is a **non-inverting** amplifier:
- R13 (1K) connects the inverting input to ground/Uref
- R15 (56K or 68K) is the feedback resistor from output to inverting input
- Normal RX gain = 1 + R15/R13 = **57× or 69×**

U4B (ADG444 section) is wired in parallel across R15. When U4B is **closed** (control HIGH):
- R15 is shorted to ~0Ω
- Gain = 1 + 0/R13 = **1× (unity gain)**

U4B is closed during the TX pulse and blanking period to prevent U3A from being driven into saturation by the large residual signal. Op-amp saturation recovery time can extend into the sample window and corrupt the measurement; unity gain mode keeps U3A in its linear region throughout. U4B opens at the end of the blanking period, restoring full gain just as the valid coil decay signal arrives.

**Combined gain U3D × U3A during RX = −10 × 57 = −570×**

### 5. DC Servo — U3C (no reset, continuous operation)

With −570× gain in the chain, sub-millivolt DC offsets from op-amp bias currents or thermal drift would drive the output to rail. U3C is a continuous DC servo that prevents this.

Configuration:
- Non-inverting input: Uref (sets the target DC operating point)
- Inverting input: receives a sample of the chain output
- Feedback: R14 (2M0–2M7 adjustable) in parallel with C3 (0.1µF)
- Time constant τ = R14 × C3 ≈ 0.24 seconds at mid-range

U3C integrates only the DC and near-DC component of the signal (pole at ~0.66 Hz) and feeds a correction voltage back to a summing point early in the chain (at or before U3D's input), continuously steering the long-term DC average toward Uref.

**U3C has no reset.** Resetting it would cause the chain to drift before the servo re-established correction.

**R14 is the single user-facing sensitivity trimmer.** It sets the servo's bandwidth:
- Lower R14 → faster servo → more aggressive DC correction → lower sensitivity
- Higher R14 → slower servo → tolerates more drift → higher sensitivity but risk of instability

The standard tuning procedure ("advance until self-oscillation, then back off") finds the edge where servo bandwidth approaches detection signal bandwidth.

### 6. Per-Pulse Integrator — U3B, reset by U4C

U3B is the measuring integrator:
- Input: R17 (3K)
- Feedback capacitor: C5 (2200µF or 1500µF)
- Time constant τ = R17 × C5 = ~6.6 seconds >> sample window duration (~350µs)
- Because τ >> sample window, output ramps linearly ∝ accumulated input charge

U4C (ADG444 section) is wired across C5. When U4C is briefly asserted (control HIGH), C5 is shorted and the integrator resets to zero. The ATmega8 pulses U4C after each ADC read, immediately before the next TX pulse.

During the RX sample window, U4D connects the receive path, U3B integrates the decaying coil signal, and the integrated value grows proportionally to the signal energy. Metal near the coil extends the decay time constant (eddy currents), resulting in a higher integrated value. The ATmega8 ADC reads U3B's output (via U_ADC net) and compares it against a software threshold to determine detection.

---

## U4 (ADG444) Switch Control Summary

All four ADG444 sections are active-HIGH (switch closes when digital control = HIGH).

| Switch | Controls | HIGH (closed) | LOW (open) |
|--------|----------|---------------|------------|
| U4A | VT1 gate | TX pulse ON — coil energized | TX OFF |
| U4B | R15 across U3A | Unity gain (1×) on U3A | Full gain (57×) on U3A |
| U4C | C5 across U3B | Integrator reset | Integrator active |
| U4D | RX signal path | Receive path connected | Receive path disconnected |

---

## Timing Sequence (One Full Cycle, ~2ms at 500Hz PRF)

| Phase | U4A | U4B | U4C | U4D | Duration |
|-------|-----|-----|-----|-----|----------|
| TX pulse | HIGH | HIGH | LOW | LOW | ~200µs |
| Blanking | LOW | HIGH | LOW | LOW | ~50µs |
| RX sample window | LOW | LOW | LOW | HIGH | ~350µs |
| ADC hold / integrator reset | LOW | LOW | HIGH | LOW | ~50µs |
| Quiet period | LOW | LOW | LOW | LOW | ~1.4ms |

Note: exact durations are firmware-defined and tunable. The blanking period must exceed the coil ring-down time. The sample window start delay (blanking end) is the most sensitivity-critical timing parameter — earlier sampling captures more signal energy but risks residual blanking artifacts.

---

## DC Supply

Battery → VD3 (1N5819, reverse polarity protection) → U2 (78L05) → +5V logic rail. The op-amp chain runs on a split supply (VDD / V−) derived separately. Uref (~4.5V) is generated by U5 (TL431) with R19/R20 voltage divider and distributed as a reference to all op-amp non-inverting inputs not carrying signal.

---

## Notes for Porting to a Different Microcontroller

- All four U4 control signals are digital outputs, active HIGH
- U4B must go HIGH simultaneously with or before U4A (unity gain before TX fires)
- U4B must go LOW at end of blanking, simultaneously with U4D going HIGH
- U4C reset pulse needs only be long enough to discharge C5 — a few microseconds suffices
- U_ADC is the analog input — ensure ADC reference matches Uref level or rescale in firmware
- The tuning trimmer R14 is hardware — no firmware equivalent needed
- C15 (470pF across U3B input) is marked "do not install" in the original design
