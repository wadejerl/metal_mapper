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
 * trade can be found on the bench. 120 µs (the old fixed value) is the cap. */
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

/* All 8 analog inputs, converted every cycle regardless of fire mode
 * (adjacent coils read each other's pulses — cross-interpolation data).
 * Each value is the hardware SUM of 16 12-bit conversions (0..65520,
 * see md_adc_init): ~2 extra effective bits from oversampling.
 * Order: [0]=PA0 [1]=PA1 (ADC1), [2]=PA6 [3]=PA7 (ADC2),
 *        [4]=PB1 [5]=PB13 (ADC3), [6]=PB12 [7]=PB14 (ADC4). */
#define MD_ADC_COUNT 8U
extern volatile uint16_t md_adc[MD_ADC_COUNT];
extern volatile uint8_t  md_adc_frame_ready;   /* set per frame; pacer re-clears */

/* GPS-synchronised cycle pacing (TIM7 one-shot, 0.1 ms ticks).
 *
 * The GPS fix interval is the master cadence: every valid GGA re-arms TIM7
 * so the next cycle completes MD_SAMPLE_LEAD_MS before the following fix
 * arrives — each CSV line then carries a frame measured just before its
 * fix. Without a valid fix the timer re-arms itself at MD_IDLE_PERIOD_MS,
 * so idle lines stay fresh and the coils rest between samples (duty cycle
 * is the point: the front end runs warm when cycles free-run).
 *
 * MD_SAMPLES_PER_GPS_PERIOD > 1 spreads that many cycles evenly across the
 * fix interval (the last still lands MD_SAMPLE_LEAD_MS early) for future
 * interpolation work — the extra frames overwrite md_adc[] and are NOT
 * reported; consuming them needs host-side changes too. */
#define MD_GPS_PERIOD_MS          50U   /* Quectel fix interval (20 Hz)    */
#define MD_SAMPLES_PER_GPS_PERIOD 1U    /* cycles per fix interval (>= 1)  */
#define MD_SAMPLE_LEAD_MS         2U    /* cycle-to-fix lead time          */
#define MD_IDLE_PERIOD_MS         500U  /* no-fix sampling/reporting rate  */

#if MD_SAMPLES_PER_GPS_PERIOD < 1U
# error "MD_SAMPLES_PER_GPS_PERIOD must be >= 1"
#endif
#if (MD_GPS_PERIOD_MS % MD_SAMPLES_PER_GPS_PERIOD) != 0U
# error "MD_SAMPLES_PER_GPS_PERIOD must divide MD_GPS_PERIOD_MS (1, 2, 5, 10)"
#endif
#if (MD_GPS_PERIOD_MS / MD_SAMPLES_PER_GPS_PERIOD) <= MD_SAMPLE_LEAD_MS
# error "MD_SAMPLES_PER_GPS_PERIOD too high: first fire delay would be <= 0"
#endif

/* Set by the pacer when an idle-cadence cycle starts (no valid fix armed
 * the timer); with md_adc_frame_ready it tells the app task to emit an
 * idle CSV line from the fresh frame. Consumer clears md_idle_line_due;
 * md_adc_frame_ready is re-cleared by the pacer at the next cycle start. */
extern volatile uint8_t md_idle_line_due;

/* Re-sync the pacer to a just-received valid GGA fix. USART2 IRQ context;
 * TIM7 runs at the same NVIC priority so the two arm sites never nest. */
void md_pace_on_fix(void);

/* TIM7 expiry hook — called from TIM7_DAC_IRQHandler in stm32g4xx_it.c.
 * Starts the cycle and re-arms (fix-interval subdivision or idle rate). */
void md_pace_fired(void);

/* Which coil(s) fire this cycle. This gates ONLY the TX channels: the
 * gain/integ/RX signals on both sets run the identical dual-mode timeline
 * every cycle, and all 8 ADC inputs are read regardless. Single-coil
 * operation alternates MD_FIRE_A / MD_FIRE_B on successive cycles. */
typedef enum {
  MD_FIRE_A    = 1,
  MD_FIRE_B    = 2,
  MD_FIRE_BOTH = 3,
} md_fire_t;

/* Which set(s) the pacer fires each cycle (keys 1/2/3, persisted with the
 * settings record). Console vocabulary: 1 = odd (set A, PA5 strobes),
 * 2 = even (set B, PB4), 3 = both (default). Gates ONLY the TX strobes —
 * all other control lines and all 8 ADC reads run identically regardless.
 * uint8_t (md_fire_t values) so the pacer-ISR read is one atomic load. */
extern volatile uint8_t md_fire_mode;

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
 * ('S') suspends the TIM7 pacer and waits out any cycle in flight before
 * the flash write (an expiry that lands during it pends and runs after).
 * Convention: QWERTY column pairs raise/lower a value (top key raises);
 * capitals are saves/toggles/specials.
 *   a/z blanking +-2us (0..200)      s/x rx window +-1us (1..50)
 *   d/c coil spacing +-10mm (50..5000)  f/v tx pulse +-1us (10..120)
 *   1/2/3 fire mode: odd (A) / even (B) / both
 *   S save   D restore+save defaults   I info line
 *   G toggle GPS passthrough (# PQTMTXT/PQTMTAR/THS lines, default off) */
void md_console_poll(void);

/* Print the '# info ...' identity line (fw hash, GPS module version/ID,
 * timing, coil geometry, adc order). The daemon requests it with the 'i'
 * key when it connects and stamps the values into the study header. */
void md_print_info(void);

/* Print the console key map ('# keys:' lines) — boot reminder so a console
 * user doesn't need the source to recall the bindings above. */
void md_print_keys(void);

/* 1 Hz system status (ADC5 — not used by the detector circuit): PA8 rail
 * through its 10k/1k divider (volts) and die temperature (deg C).
 * Blocking ~18 us injected read; task context only. */
void md_status_read(float *vin_volts, float *temp_c);

/* Pacer IRQ context (md_pace_fired): arm both sets (TX gated by `which`)
 * and start the master. All timers start on one hardware trigger, aligned
 * to a single timer clock (~6 ns). Returns 1 if the cycle started, 0 if
 * skipped because the previous cycle (timers or ADC) is still running —
 * alternating callers should only advance on 1. */
uint8_t md_start_cycle(md_fire_t which);

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
