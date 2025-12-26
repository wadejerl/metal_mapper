/*
 * metal_detector.h - Metal detector pulse and capture module
 * Timer-based pulse generation and comparator capture
 */

#ifndef METAL_DETECTOR_H
#define METAL_DETECTOR_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "stm32h7xx_hal.h"

// ============================================================================
// Metal Detector State
// ============================================================================
extern volatile uint8_t comp1_captured;
extern volatile uint32_t comp1_capture_time_charge;
extern volatile uint32_t comp1_capture_time_discharge;

// ============================================================================
// Metal Detector Functions
// ============================================================================

/**
 * @brief Trigger metal detector pulse using TIM2
 * @param pulse_width_us Pulse width in microseconds
 */
void TIM2_Trigger_Pulse(uint32_t pulse_width_us);

/**
 * @brief Arm TIM3 input capture for comparator timing
 */
void TIM3_arm_capture(void);

#ifdef __cplusplus
}
#endif

#endif /* METAL_DETECTOR_H */
