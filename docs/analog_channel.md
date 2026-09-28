# Analog channel — Metal Mapper detector3 board, as built

Source of truth for this description: the board's Altium schematic (9
sheets: `metal_mapper_Top.SchDoc` plus eight copies of `analog.SchDoc`,
sheets 2.1–2.8, reference suffixes A–H — the PDF is to live under `docs/`
with the gerbers and the rest of the hardware package) and the firmware in
`stm_detector3/Core/Src/metal_detector.c`. The channel descends from the
ClonePI-W idea (TL074 chain, switched integrator), but the values, the
switch logic, the transistor and the supplies all differ from the ATmega
original — take the numbers from here, not from a ClonePI-W write-up.

## 1. Channel numbering, sheets, pins

The top sheet carries the note: "Yes, the Chan numbering and enumeration are
reversed. The Ch<nn> numbers match the UI." Altium's `An1..An8` nets count
the other way from the `Ch0..Ch7` sheet symbols. **`Ch k` is what the UI, the
wire protocol (`adc k`) and the firmware (`md_adc[k]`) all call channel k**;
`An n` is only the net name, `An n = Ch (8−n)`.

| Ch (UI / fw `md_adc[k]`) | Sheet | Net  | MCU pin | ADC input | Control bus | Position (top-sheet note) |
|---|---|---|---|---|---|---|
| Ch0 | 2.8 (H) | An8 | PA0  | ADC1_IN1 | A | right-most |
| Ch1 | 2.7 (G) | An7 | PA1  | ADC1_IN2 | B |            |
| Ch2 | 2.6 (F) | An6 | PA6  | ADC2_IN3 | A |            |
| Ch3 | 2.5 (E) | An5 | PA7  | ADC2_IN4 | B |            |
| Ch4 | 2.4 (D) | An4 | PB1  | ADC3_IN1 | A |            |
| Ch5 | 2.3 (C) | An3 | PB13 | ADC3_IN5 | B |            |
| Ch6 | 2.2 (B) | An2 | PB12 | ADC4_IN3 | A |            |
| Ch7 | 2.1 (A) | An1 | PB14 | ADC4_IN4 | B | left-most  |

The top sheet draws the eight channel blocks in a column with "Left side" at
the Ch7 end, "Right side" at the Ch0 end and a "Vehicle Direction" arrow, so
looking forward along the vehicle Ch7 is the left-most coil and Ch0 the
right-most. Each ADC converts one adjacent pair (ADC1 = Ch0/Ch1 … ADC4 =
Ch6/Ch7), which is what lets all eight be read in one ~45 µs injected
sequence. Even channels hang on **Bus A**, odd channels on **Bus B**
(firmware `MD_CH_SET_B(k) = k & 1`), so every ADC pair holds one coil of each
bus.

Each bus is four wires from the MCU, shared by its four channel sheets:

| Function (sheet port) | Bus A (Ch0/2/4/6) | Bus B (Ch1/3/5/7) |
|---|---|---|
| `PULSE_TRIG` — Q1 gate (coil TX) | PA5, TIM2_CH1, R17 10 kΩ pull-down | PB4, TIM3_CH1, R16 10 kΩ pull-down |
| `GAIN_CUT` — ADG444 IN2          | PA9, TIM1_CH2                      | PA15, TIM8_CH1                     |
| `INTEGRATOR_RST` — ADG444 IN1    | PA10, TIM1_CH3                     | PB9, TIM8_CH3                      |
| `SIGNAL_PASS` — ADG444 IN4       | PA11, TIM1_CH4 (CC4P inverted)     | PC13, TIM8_CH4N (CC4NP inverted)   |

Also on the top sheet: PA8 = ADC5_IN1 reads `Vin` through R18 10 kΩ / R19
1 kΩ (÷11, firmware `MD_STATUS_DIVIDER 11.0`); the Pi comes in on J1
(Phoenix 8-pin: `Vin`, grounds, MCU USART1 PB6/PB7 through 1 kΩ and a TVS
array D3, plus a pass-through pair to the GPS module's second UART through
20 Ω); the GPS module M1 (labelled `LG280P` on the sheet; the firmware talks
to a Quectel LG290P) is on USART2 PA2/PA3; JP2 is SWD, S1 BOOT0, S2 NRST.

## 2. One channel, left to right (sheet 2.x, suffix letter dropped)

### 2.1 Coil supply — `ISO_B+`

`V+` → R4 **20 Ω** → `ISO_B+`, decoupled by C2 ‖ C11 = **2 × 470 µF, 16 V**
(940 µF). Every channel has its own `ISO_B+` node; the 20 Ω keeps the pulse
current of one coil out of the other seven and out of the op-amp rail. Two
consequences worth keeping in mind: the reservoir sags by roughly
½·I₀·t_tx / 940 µF during a pulse (tens to ~100 mV), and the **average** coil
current drops `ISO_B+` below `V+` by 20 Ω × I_avg, where I_avg ≈ f × ½·I₀·t_tx
— so the coil's charging voltage, and hence I₀ and the flyback, fall as the
pulse rate (or raster's 4× bus rate) goes up. That is the "coil current
collapses with rate" seen on the bench.

`V+` is drawn as U4, an LM2940-10 (10 V LDO) with C7 1000 µF, but the sheet
carries the red note "Changed to adjustable buck set for 12.8 V": **V+ =
12.8 V as built**. It powers the TL074s and the ADG444's VDD.

### 2.2 TX switch and coil — Q1, JP1, R7

Q1 is an **IRLML2502** (20 V, 4.2 A, logic-level, SOT-23). Gate = the bus
`PULSE_TRIG` line straight from the MCU pin (3.3 V, no series resistor; four
gates per bus, one 10 kΩ pull-down per bus on the top sheet). Source = GND,
drain = JP1 pin 1. The coil connects between **JP1-2 (`ISO_B+`) and JP1-1
(drain)**; JP1-3 is GND (shield). **R7 = 390 Ω sits directly across the
coil** as its damping resistor.

While Q1 is on the drain is at ~0 V and the coil charges from `ISO_B+`. At
turn-off the coil current has to keep flowing into the drain node, so the
drain flies **above** `ISO_B+`. With a 390 Ω damper the resistor alone can
absorb it only while I₀ × 390 Ω < (Q1's breakdown − `ISO_B+`), i.e. below a
few tens of mA; for any real PI coil current Q1 goes into avalanche and the
drain is clamped near its breakdown (≥ 20 V) while the current ramps down at
(V_clamp − `ISO_B+`)/L. Only the tail is set by R7 (τ = L/R7). Coil L and I₀
are not on the schematic, so treat this as analysis; it is consistent with
the bench "flyback cliff" (blanking below ≈ 46–52 µs at TX 100 µs breaks the
channels, the edge moving with TX pulse and with pulse rate).

### 2.3 Pick-off and clamp — R8, D1, D2

R8 **390 Ω** from the drain to `Coil_Sig_clamped`, with **D1 and D2
anti-parallel between that node and `ISO_B+`**. So the amplifier input is
held within about ±0.6 V of `ISO_B+`: during TX (drain at 0) it sits at
`ISO_B+` − 0.6 V; during the flyback it sits at `ISO_B+` + 0.6 V; the useful
receive signal is the last few mV of the decay **above `ISO_B+`**, and a
target (eddy currents keeping the flux up) makes that positive tail last
longer. Signal polarity through the rest of the chain follows from this.

### 2.4 First stage — U3D, "10x gain"

TL074 section D, supplied `V+`/GND. Non-inverting input (pin 12) is the
summing node `COIL_AMP`: R9 **1 kΩ** from `Coil_Sig_clamped` and R10
**10 kΩ** from the DC servo output. The inverting input (pin 13) sees R6
**1 kΩ to `ISO_B+`** and R2 **10 kΩ** feedback from the output (pin 14,
`Pri_Stg_Out`). The sheet notes "Add 1k Pot" beside R6 (a gain-trim idea,
not fitted).

The feedback divider is referenced to `ISO_B+`, not ground, so:

    V_pri = 11·V_p − 10·ISO_B+          with  V_p = (10·V_clamped + V_servo)/11
          = V_servo + 10·(V_clamped − ISO_B+)

i.e. exactly **10× on the coil signal** (the label) referenced to the coil's
own supply, plus the servo output at unity gain. The `ISO_B+` term cancels
out completely — the stage's output does not know what the coil rail is.

### 2.5 Second stage — U3A, "56x gain", and the gain-cut switch

Non-inverting, referenced to **`Vdda` (3.3 V)**: pin 3 = `Pri_Stg_Out`,
pin 2 = `Sec_Stg_In` with R5 **1 kΩ to `Vdda`** and R3 **56 kΩ** feedback
from pin 1 (`Sec_Stg_Out`). Gain = 1 + 56k/1k = **57×** (labelled 56x).
ADG444 switch **S2/D2 (IN2 = `GAIN_CUT`) is across R3**: switch closed →
follower (1×), switch open → 57×. Sheet note: "when enabled, forces 56x gain
stage to 1x avoiding saturation during initial coil discharge".

    V_sec = Vdda + G·(V_pri − Vdda),   G = 57 (released) or 1 (cut)

With `V+` = 12.8 V the stage clips at roughly 11 V, i.e. about 8 V above
`Vdda` → the 57× stage is linear only for coil-referred tails below
≈ 14 mV. Everything earlier in the decay is clipped, which is why the gain is
cut until the window opens.

### 2.6 DC servo — U3C

TL074 section C as an integrator: pin 10 (+) = `Vdda`; pin 9 (−) via R11
**2 MΩ** from `Sec_Stg_Out`; C3 **0.1 µF** from output (pin 8, `SRVO_OUT`)
back to pin 9. τ = R11·C3 = **0.2 s**. Its output enters the first stage's
summing node through R10 and, by the algebra above, appears at `Pri_Stg_Out`
with gain 1. The loop drives the **time-average of `Sec_Stg_Out` to `Vdda`**,
which parks the whole chain at the 3.3 V operating point regardless of op-amp
offsets and the ±0.6 V clamp excursions ("DC Servo to avoid drift").

Because the second stage runs at 57× only during the window (+ the ~45 µs ADC
hold) and at 1× the rest of the cycle, the average is weighted: at 200 Hz
with a ~55 µs high-gain span the window carries ≈ 3.1 ms-equivalent against
≈ 4.85 ms of idle. Two approximate consequences (from the values, not
measured): the loop settles with τ ≈ 0.2 s / 1.6 ≈ 0.13 s, and a steady
target/residual in the window is cancelled to about 40 % in the first stage
(steady-state response ≈ 60 % of the instantaneous one). Placing a static
target should show the reading relax over a few tenths of a second; worth a
bench check.

### 2.7 Signal switch and integrator — S4, R1, C1, U3B, S1

ADG444 **S4/D4 (IN4 = `SIGNAL_PASS`)** connects `Sec_Stg_Out` to
`Sig_to_int`, then R1 **3 kΩ** into the inverting input (pin 6) of TL074
section B; C1 **2.2 nF 0603** from pin 6 to the output (pin 7 =
`ANALOG_OUT` = `An n`); pin 5 (+) = `Vdda`. **S1/D1 (IN1 = `INTEGRATOR_RST`)
is across C1**: closed → capacitor shorted, output = `Vdda`; open →
integrating (switch S4 closed) or holding (S4 open).

    τ_int = R1·C1 = 3 kΩ × 2.2 nF = 6.6 µs   (+ ~70 Ω switch R_on → ≈ 6.75 µs)
    V_out = Vdda − (1/τ_int) · ∫_window (V_sec − Vdda) dt
          = Vdda − (570/6.6 µs) · ∫_window [ v_coil(t) + (V_servo − Vdda)/10 ] dt

Sheet note: "when enabled, the final output integrator is reset. This must be
opened at exactly the same time #4 closes" — the firmware does this with a
single compare value (§3). Sheet note on S4: "Connects the gain stages to the
integrator. SW will adjust timing to move sensitivity window around."

Scale, as run (10 µs window): a 1 mV coil-referred tail held over the window
moves the output by 570 × 1 mV × 10 µs / 6.6 µs ≈ **0.86 V ≈ 17 k counts**.
Usable range is `Vdda` = 3.3 V (reset) down to the TL074's low-side swing,
≈ 1.2–1.3 V on this single 12.8 V/GND supply → about 2 V ≈ 40 k counts ≈ a
2.4 mV mean tail at rx = 10 µs. S3 (IN3) is unused.

### 2.8 ADG444 (U2) and the supplies

U2 ADG444BRZ: VDD (13) = `V+`, VSS (4) = GND, GND (5) = GND, **VL (12) =
3v3**. Datasheet (ADG441/442/444, Analog Devices): each ADG444 switch **turns
on when a logic low is applied** (IN = 0 → ON, 1 → OFF; the ADG442 is the
opposite); VINH 2.4 V min / VINL 0.8 V max; R_on ≈ 70 Ω typ (110 Ω max at
VDD = 10.8 V); "fully specified with a single +12 V power supply"; analog
range 0 to VDD; break-before-make. The ADG444 is the VL-pin variant, which
the datasheet specifies at **VL = 5 V**; this board runs VL at 3.3 V and
drives IN from 3.3 V logic (works on the bench; noted as outside the
characterised condition).

Rails: `Vin` from the Pi → U5 AMS1117-3.3 → `3v3` (C8 1000 µF) → FB1 ferrite
(1 kΩ @ 100 MHz) → `Vdda` (C9 1000 µF + C10 0.1 µF). `Vdda` feeds the MCU's
VDDA **and VREF+ (pins 21/20 tied)** and is the reference for U3A, U3B and
U3C. The ADC is therefore ratiometric to the same node the integrator resets
to: **a reset integrator reads exactly full scale (65520 = 16 × 4095) whatever
the 3.3 V rail actually is, and signal pulls the count down.** The TL074
inputs sit only 3.3 V above their negative rail — the TL07x common-mode range
reaches to about 3–4 V above V−, so this is at the edge; worth checking
against the datasheet if a rail change is ever considered.

## 3. Control timeline (firmware, all hardware-timed)

TIM2 is the master; TIM3 (Bus B TX) and TIM1/TIM8 (bus A/B auxiliaries) are
trigger-slaved, so one `CEN` write starts everything and every edge below is
a compare/update event at 170 MHz (1 tick = 5.9 ns). Values in µs; "as run" =
the September 2026 200 Hz setting (TX 100, blanking 51–52, RX 10); "compiled
default" = `MD_TX_PULSE_US 120`, `MD_BLANKING_US 16`, `MD_RX_WINDOW_US 3`
(500 Hz).

| t | Event | Pins | Switches | as run | default |
|---|---|---|---|---|---|
| 0 (+1 tick) | trigger; TX HIGH → Q1 on, coil charges | PA5/PB4 ↑ | S1, S2 closed (reset, 1×); S4 open | 0 | 0 |
| tx | TX LOW → flyback, then blanking | PA5/PB4 ↓ | unchanged | 100 | 120 |
| tx + bl (+1 tick) | window opens: gain released, reset opens, signal connected — **one compare value on the aux timer** | PA9/PA15 ↑, PA10/PB9 ↑, PA11/PC13 ↓ | S2 open (57×), S1 open, S4 closed | 152 | 136 |
| tx + bl + rx | window closes: signal disconnected, integrator **holds**; TIM1 update kicks all four ADCs | PA11/PC13 ↑ | S4 open | 162 | 139 |
| + ≈ 45 | four ADCs finish their 2-channel injected sequences (47.5-cycle sampling, 16× oversampled sums); the last JEOS cuts gain and resets the integrator | PA9/PA15 ↓, PA10/PB9 ↓ | S2 closed (1×), S1 closed | ≈ 207 | ≈ 184 |
| 1/f | next cycle from the TIM7 pacer (rates 20/40/100/200/500 Hz) | | | 5000 | 2000 |

Console keys: `a`/`z` blanking 0–200, `s`/`x` RX window 1–50, `f`/`v` TX
pulse 10–120; tx + bl + rx ≤ 385 µs (16-bit ARR). TX uses PWM mode 2 with
CCR = 1 (ref LOW while the timer sits stopped, so idle pins are safe); the
RX-connect outputs use the same trick for the window; gain and reset use
Active-on-compare, which latches HIGH through the one-pulse stop — exactly the
hold the integrator needs. Both buses fire every cycle in normal mode. In
raster mode the pacer runs eight slots, each firing only one channel's bus
with that channel's own {tx, bl, rx} while the other bus is parked
force-inactive (coils off, integrators in reset, signal switches open).

### Firmware polarity vs. ADG444 logic

| Signal | Pin idle | Pin in window | Switch idle | Switch in window | Firmware comment |
|---|---|---|---|---|---|
| `GAIN_CUT` → IN2/S2 (across R3) | LOW | HIGH | closed → 1× | open → 57× | "gain-protect release" |
| `INTEGRATOR_RST` → IN1/S1 (across C1) | LOW | HIGH | closed → reset | open → integrate/hold | "integrator enable" |
| `SIGNAL_PASS` → IN4/S4 (to R1) | HIGH (CC4P/CC4NP inverted) | LOW | open → isolated | closed → connected | "RX connect, inverted pol" |

All three agree with the active-low ADG444: the firmware's idle levels give
"integrator in reset, gain cut, signal disconnected", and the window flips
all three at once.

## 4. What the numbers mean

- **Idle = full scale, metal = lower.** A reset integrator reads 65520; the
  residual coil tail and any target pull the count down. Field and bench data
  agree (2026-09-19 tuning: "target pushes readings DOWN, away from the 65535
  rail"; in a 2026-09-04 study several channels bottom out at ≈ 25.1 k counts,
  none goes below 25.0 k, and one touches 65520).
- **Floor ≈ 24.5–25 k counts ≈ 1.23–1.26 V** = the TL074 output's low-side
  limit on this supply, not an ADC or firmware limit. A channel "floors" when
  its mean tail over the window exceeds ≈ 2.4 mV (rx = 10 µs).
- **Channel offsets** (ch7 idling ~+15 k above the array mean, ch3 ~−13 k
  below, drifting several k per hour) are most likely differences in the coils' late-time
  residual inside the window (coil L/R/damping, temperature, `ISO_B+` sag
  through R4), not gain differences — responses to the same target are within
  ±25 % while noise varies 1.7×.
- **Rate dependence** of the tail and of the flyback cliff follows from §2.1
  (20 Ω × I_avg) and §2.2 (avalanche-clamped flyback lasting ∝ L·I₀).
- The servo (§2.6) makes the chain a slow high-pass (τ ≈ 0.1–0.2 s): part of
  a stationary target's response decays away; a moving survey is largely
  unaffected but expect a small opposite-sign overshoot after a pass.

## 5. Observations from the schematic (not changes — for the record)

1. ADG444 VL is 3.3 V against a 5 V datasheet condition (§2.8).
2. IN3 (unused switch) is left open on the sheet; CMOS logic inputs are
   usually tied off.
3. Q1 absorbs the coil energy in avalanche every pulse (20 V part on a
   12.8 V rail with a 390 Ω damper) — see §2.2; the IRLML2502 is
   avalanche-rated but it is the hottest part on the board by design.
4. TL074 inputs at 3.3 V are at the bottom of the TL07x common-mode range
   (§2.8).
5. The sheet still shows the LM2940-10 and the `LG280P` label; the board has
   the 12.8 V buck and the firmware speaks to an LG290P.

Related: `docs/detector3.md` (firmware/protocol), `README.md` (sample row
format, `adcN` = 16× oversampled sum 0–65520).
