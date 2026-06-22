/*
 * metal_detector.h - ADC-based PI metal detector sequencer
 *
 * 3-ISR timing chain (all times from md_start_cycle CEN↑):
 *
 *   Foreground md_start_cycle():
 *     PA0 HIGH (TX on). TIM5 starts counting.
 *
 *   t = MD_TX_PULSE_US  ── ISR 1 (TIM5 CC1, md_tim5_cc1_fired):
 *     PA0 LOW (TX off). TIM5 stopped. TIM1 loaded: CH2/CH3/CH4 all set to
 *     Active-on-compare with CCR = md_blanking_us. TIM1 starts (OPM).
 *
 *   t = TX + md_blanking_us  ── hardware compare (no ISR):
 *     CH2/CH3/CH4 compare fires simultaneously → aligned leading edges:
 *       PE11 (U4B) HIGH  gain restored
 *       PE13 (U4C) HIGH  integrator enabled (charge accumulation begins)
 *       PE14 (U4D) LOW   RX path connected  (CC4P=1 inverts OC4REF)
 *
 *   t = TX + blanking + md_rx_window_us  ── ISR 2 (TIM1 UIE, md_tim1_uie_fired):
 *     TIM1 OPM expires. PE14 (U4D) → HIGH (RX path disconnected).
 *     PE13 (U4C) stays HIGH — integrator holds sampled charge for ADC.
 *     ADC conversion started in interrupt mode.
 *
 *   t ≈ TX + blanking + window + ~1 µs  ── ISR 3 (ADC EOC, md_adc_eoc_fired):
 *     PE11 (U4B) → LOW, PE13 (U4C) → LOW (integrator reset).
 *     ADC result stored. md_adc_result_ready flag set.
 *
 * Pin mapping (TIM1 OC outputs — all AF, no GPIO writes in ISRs):
 *   PE11 / CN10-6  = U4B  TIM1 CH2  gain reduce / protect U3A  (CC2P=0)
 *   PE13 / CN10-10 = U4C  TIM1 CH3  integrator enable           (CC3P=0)
 *   PE14 / CN10-8  = U4D  TIM1 CH4  RX path connect             (CC4P=1)
 *   PA0  / CN10-29 = U4A  TIM5 CH1  TX coil energize            (CC1P=0)
 */

#ifndef METAL_DETECTOR_H
#define METAL_DETECTOR_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "stm32h7xx_hal.h"

/* ============================================================================
 * Timing parameters — adjust on the bench.
 * All values are timer ticks (1 tick = 1 µs at PSC=199, 200 MHz APB timer clk).
 * ========================================================================== */
#define MD_TX_PULSE_US       120U   /* TIM5 CCR1: TX coil on for this many µs  */
#define MD_BLANKING_US        16U   /* Default blanking after TX (adjust a/z)   */
#define MD_RX_WINDOW_US        3U   /* Default RX window width (adjust s/x)     */

/* ============================================================================
 * Flash settings storage — last 128KB sector (sector 7, 0x080E0000).
 * Auto-loaded at startup; saved by calling MD_Save_Settings() (key 'S').
 * ========================================================================== */
#define MD_SETTINGS_FLASH_ADDR  0x080E0000UL   /* sector 7 base address        */
#define MD_SETTINGS_MAGIC       0x4D443001UL   /* "MD" config version 1        */

/* ============================================================================
 * Public API
 * ========================================================================== */

/* Call once from main() USER CODE BEGIN 2, after all MX_ inits complete.      */
void MD_Hardware_Init(void);

/* Kick off one detection cycle.  No-op if TIM5 is still running.              */
void md_start_cycle(void);

/* Returns 1 and fills *result if an ADC conversion is ready; clears the flag. */
uint8_t md_get_result(uint32_t *result);

/* Load settings from flash (called by MD_Hardware_Init at startup).           */
void MD_Load_Settings(void);

/* Erase sector 7 and write current timing values to flash.  Call from task
 * context only — erase takes ~2 s and stalls this task during that time.     */
void MD_Save_Settings(void);

/* ISR dispatch functions — called from HAL callbacks in main.c.               */
void md_tim5_cc1_fired(void);   /* ISR 1: TIM5 CC1 (PA0-end event)             */
void md_tim1_uie_fired(void);   /* ISR 2: TIM1 UIE/OPM (RX window end)         */
void md_adc_eoc_fired(void);    /* ISR 3: ADC EOC (conversion complete)         */

extern volatile uint8_t  md_adc_result_ready;
extern volatile uint32_t md_adc_result;

/* Runtime-adjustable timing (µs).  Write from task context only; ISR1 reads
 * these at the start of each cycle to load TIM1 registers. */
extern volatile uint32_t md_blanking_us;    /* blanking window after TX         */
extern volatile uint32_t md_rx_window_us;   /* RX/integration window width      */

/* DWT cycle-counter timestamps captured in ISRs (CPU cycles at 400 MHz).
 * Divide differences by 400 to convert to µs. */
extern volatile uint32_t md_ts_cen;   /* TIM5 CEN set (md_start_cycle)          */
extern volatile uint32_t md_ts_tim5;  /* TIM5 CC1 ISR fired                     */
extern volatile uint32_t md_ts_tim1;  /* TIM1 UIE ISR fired                     */

#ifdef __cplusplus
}
#endif

#endif /* METAL_DETECTOR_H */
