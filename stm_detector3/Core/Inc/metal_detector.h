/*
 * metal_detector.h - PI metal detector sequencer (stm_detector3, STM32G474)
 *
 * Two sets of control signals, four each:
 *   Set A: TIM2 CH1 (PA5, TX coil) + TIM1 CH2/CH3/CH4 (PA9/PA10/PA11)
 *   Set B: TIM3 CH1 (PB4, TX coil) + TIM8 CH1/CH3/CH4N (PA15/PB9/PC13)
 *
 * Ported from stm_detector2 (STM32H723, single channel). Same 3-ISR
 * structure; timers parameterized per set. Both sets implemented; the
 * ADC reads land in a later step.
 */

#ifndef METAL_DETECTOR_H
#define METAL_DETECTOR_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "stm32g4xx_hal.h"

/* Cycle timing defaults (µs), identical to stm_detector2 */
#define MD_TX_PULSE_US    120U
#define MD_BLANKING_US    16U
#define MD_RX_WINDOW_US   3U

/* Runtime-tunable timing (adjusted via console keys, like detector2).
 * md_tx_pulse_us is the coil charge time: longer charges push more energy
 * into the coil but heat the TX FETs — tunable so the thermal/sensitivity
 * trade can be found on the bench. 120 µs (the old fixed value) is the cap.
 *
 * These three globals drive the SIMULTANEOUS (all-8-at-once) mode only.
 * Raster mode has its own per-channel table inside metal_detector.c —
 * the two never mix, so toggling modes can't mangle either tuning. */
extern volatile uint32_t md_tx_pulse_us;
extern volatile uint32_t md_blanking_us;
extern volatile uint32_t md_rx_window_us;

/* Coil-array geometry, provisioned per unit (d/c keys, 'S' save) and
 * reported by the 'I' info line. The visualizer places each coil from
 * these plus the GPS heading; the firmware only stores and reports. */
#define MD_COIL_SPACING_MM_DEFAULT 330U   /* 0.33 m between adjacent coils */
/* Antenna sits 0.5 m toward the port (low-ADC) end of the coil line, so
 * the array center is 0.5 m starboard of the reported position. */
#define MD_COIL_OFFSET_RIGHT_MM_DEFAULT 500
extern volatile uint32_t md_coil_spacing_mm;
extern volatile int32_t  md_coil_offset_fore_mm;   /* + forward of antenna */
extern volatile int32_t  md_coil_offset_right_mm;  /* + starboard          */

/* Completed-cycle counter (incremented when a full ADC frame lands) */
extern volatile uint32_t md_cycle_count;

/* All 8 analog inputs, converted every cycle
 * (adjacent coils read each other's pulses — cross-interpolation data).
 * Each value is the hardware SUM of 16 12-bit conversions (0..65520,
 * see md_adc_init): ~2 extra effective bits from oversampling.
 * Order: [0]=PA0 [1]=PA1 (ADC1), [2]=PA6 [3]=PA7 (ADC2),
 *        [4]=PB1 [5]=PB13 (ADC3), [6]=PB12 [7]=PB14 (ADC4).
 * Live view of the most recent frame (debug aid); reporting reads the
 * frame ring below instead, so no frame is lost while printf blocks. */
#define MD_ADC_COUNT 8U
extern volatile uint16_t md_adc[MD_ADC_COUNT];

/* Cycle pacing (TIM7 one-shot, 0.1 ms ticks) — FREE-RUNNING.
 *
 * The pacer fires every sample interval (MD_GPS_PERIOD_MS divided by
 * md_samples_per_period) and re-arms itself from its own ISR. Nothing
 * else — no GPS fix, no NMEA parse, no RTOS activity — ever touches it,
 * so a coil pulse can move only by the pacer ISR's own entry latency
 * (microseconds; it runs above every non-timeline interrupt). The stream
 * runs at the full configured rate with or without a fix: bench tools see
 * all 8 channels at rate with no GPS attached, and the analog front end
 * holds the same thermal duty cycle on the bench as in the field (it runs
 * warm — that IS the surveying operating point; an idle rest would make
 * bench baselines lie about field baselines).
 *
 * Until 2026-09-20 every valid GGA re-armed TIM7 so a frame completed
 * 1 ms before each fix. That coupled the GPS module's sentence-arrival
 * jitter into the pulse interval (post-fix interval anywhere in
 * [I-1 ms, 2I-1 ms)) and showed up as common-mode noise on all channels
 * whenever the unit had a lock — see the pacing section in
 * metal_detector.c. The host never needed the lead.
 *
 * Every completed frame is queued (see md_frame_pop) and reported as its
 * own CSV sample line carrying a sample tick; GPS fixes are reported as
 * separate anchor lines carrying the tick of the last completed frame,
 * which landed within one sample interval before the fix parsed (the
 * anchor line always follows its sample line on the wire). The host
 * interpolates sample positions between anchor ticks, so the rate is
 * fixed per study — never speed-adaptive, which would turn speed changes
 * into coil duty-cycle (thermal, baseline) changes. */
#define MD_GPS_PERIOD_MS          50U   /* Quectel fix interval (20 Hz)    */
#define MD_SAMPLES_PER_GPS_PERIOD 25U   /* default cycles per fix (500 Hz) */

#if MD_SAMPLES_PER_GPS_PERIOD < 1U
# error "MD_SAMPLES_PER_GPS_PERIOD must be >= 1"
#endif
#if (MD_GPS_PERIOD_MS % MD_SAMPLES_PER_GPS_PERIOD) != 0U
# error "MD_SAMPLES_PER_GPS_PERIOD must divide MD_GPS_PERIOD_MS (1, 2, 5, 10, 25)"
#endif

/* Runtime sample rate: cycles per GPS period, g/b console keys stepping
 * through {1, 2, 5, 10, 25} (20..500 Hz), persisted with the settings.
 * 50 (1000 Hz) is deliberately absent: sample lines would exceed the
 * 460800-baud console link (~46 kB/s). */
extern volatile uint8_t md_samples_per_period;

/* Sample ticks: the pacer numbers every fire (skipped slots burn their
 * tick, so a gap in reported ticks is a real gap in time), and each
 * completed frame carries its fire's tick. Ticks are the protocol's only
 * time axis — frames are evenly spaced (the pacer free-runs), so the host
 * interpolates positions linearly in tick space between anchors; an
 * anchor's true instant is within one interval after its tick's frame.
 * md_tick_completed is the tick of the newest completed frame; the GGA
 * path stamps it into the fix anchor (one atomic uint32 read). */
extern volatile uint32_t md_tick_completed;

/* Completed-frame ring: written by the last JEOS ISR, drained by the app
 * task (single writer / single reader, free-running indices). Sized for
 * ~64 ms of 500 Hz backlog — printf blocks ~1.3 ms per line, so depth
 * beyond a few covers only pathological stalls; an overrun drops that
 * frame (its tick gap tells the host) and counts md_frame_overruns. */
typedef struct {
  uint32_t tick;
  uint16_t adc[MD_ADC_COUNT];
} md_frame_t;

/* Dequeue the oldest completed frame into *out; 0 if the ring is empty.
 * Task context only. */
uint8_t md_frame_pop(md_frame_t *out);

/* A valid GGA just parsed. USART2 IRQ context (priority 6). READ-ONLY with
 * respect to the pacer: records where the fix fell on the sample timeline
 * and the GGA-to-GGA spacing for the 'P' diagnostics line. Never arms
 * TIM7 — the GPS must not be able to move a coil pulse. */
void md_pace_note_fix(void);

/* USART2 IRQ context: bracket an NMEA parse for the 'P' line's
 * gps_isr_max_us. _begin returns a DWT stamp (and enables the counter if
 * boot traffic gets here before md_pace_init — a raw DWT->CYCCNT read
 * with the unit still off is architecturally undefined); _end records
 * the duration since that stamp. */
uint32_t md_pace_gps_isr_begin(void);
void md_pace_note_gps_isr(uint32_t start_cyc);

/* TIM7 expiry hook — called from TIM7_DAC_IRQHandler in stm32g4xx_it.c.
 * Starts the cycle (or raster slot) and re-arms at the fixed cadence.
 * NVIC priority 4, above the FreeRTOS syscall ceiling: no RTOS calls. */
void md_pace_fired(void);

/* One-time hardware setup; call after MX_ inits, before md_start_cycle.
 * Loads saved settings from flash first. */
void MD_Hardware_Init(void);

/* Settings persistence — append-log in flash page 31 (0x0801F000).
 * Load takes the newest valid record; if none exists (new board / purged
 * page) it writes the compile-time defaults so the unit always carries a
 * valid record. Save appends a record, erasing the page only when its
 * 128 slots are full. */
void MD_Load_Settings(void);
void MD_Save_Settings(void);

/* Flash ECC double-error NMI hook — call FIRST in NMI_Handler. Returns 1
 * if the fault was in the settings page and has been absorbed (handler
 * should return); 0 for any other NMI cause (fall through). */
uint32_t MD_Settings_ECC_NMI(void);

/* Poll USART1 for tuning keys; call each task-loop iteration. A save
 * (word-gated SAVE+Enter) suspends the TIM7 pacer and waits out any cycle
 * in flight before the flash write (an expiry that lands during it pends
 * and runs after). Convention: QWERTY column pairs raise/lower a value
 * (top key raises); capitals/digits are selects/toggles/specials.
 *   a/z blanking +-1us (0..200)      s/x rx window +-1us (1..50)
 *   d/c coil spacing +-10mm (50..5000)  f/v tx pulse +-1us (10..120)
 *   g/b sample rate up/down (20/40/100/200/500 Hz)
 *   h/n raster pulse spacing faster/slower (200us..25ms step list; the
 *       slot period floor — sensitivity wants ~160/240 Hz in the field)
 *   0-7 select channel for raster tuning   8 select all channels
 *   r   toggle raster mode (per-channel slots) vs all-at-once
 *   SAVE+Enter save   CLEAR+Enter restore+save defaults   I info line
 *   G toggle GPS passthrough (# PQTMTXT/PQTMTAR/THS lines, default off)
 *   P pacer diagnostics ('# pace ...' line: measured interval deviation,
 *       GGA arrival jitter, NMEA parse time; resets the window)
 * In raster mode a/z, s/x, f/v edit the SELECTED channel's table entry
 * (or all 8 in lockstep when 'all' is selected); in all-at-once mode they
 * edit the shared globals exactly as before. */
void md_console_poll(void);

/* Print the '# info ...' identity line (fw hash, GPS module version/ID,
 * timing, coil geometry, adc order, raster mode + per-channel table). The
 * daemon requests it with the 'I' key when it connects and stamps the
 * values into the study header. Prints immediately; the 'I' key handler
 * first re-queries the GPS when its identity is still unknown and defers
 * this call until the replies land (see md_info_request in the .c). */
void md_print_info(void);

/* Print the console key map ('# keys:' lines) — boot reminder so a console
 * user doesn't need the source to recall the bindings above. */
void md_print_keys(void);

/* 1 Hz system status (ADC5 — not used by the detector circuit): PA8 rail
 * through its 10k/1k divider (volts) and die temperature (deg C).
 * Blocking ~18 us injected read; task context only. */
void md_status_read(float *vin_volts, float *temp_c);

/* Pacer IRQ context (md_pace_fired): arm both sets and start the master.
 * All timers start on one hardware trigger, aligned to a single timer
 * clock (~6 ns). Returns 1 if the cycle started, 0 if skipped because
 * the previous cycle (timers or ADC) is still running. */
uint8_t md_start_cycle(void);

/* OPM-expiry hooks (TIM1/TIM8 UIE) — called from
 * HAL_TIM_PeriodElapsedCallback in main.c. TIM1's starts the ADC reads. */
void md_tim1_uie_fired(void);
void md_tim8_uie_fired(void);

/* ADC injected-sequence IRQ hooks — called from the raw handlers in
 * stm32g4xx_it.c USER CODE 1. */
void md_adc12_irq(void);
void md_adc3_irq(void);
void md_adc4_irq(void);

#ifdef __cplusplus
}
#endif

#endif /* METAL_DETECTOR_H */
