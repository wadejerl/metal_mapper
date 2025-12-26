/*
 * metal_detector.c - Metal detector pulse and capture module
 * Timer-based pulse generation and comparator capture
 */

#include "metal_detector.h"
#include "main.h"

// External timer handles
extern TIM_HandleTypeDef htim2;
extern TIM_HandleTypeDef htim3;

// ============================================================================
// Metal Detector State Variables
// ============================================================================
volatile uint8_t comp1_captured = 0;
volatile uint32_t comp1_capture_time_charge = 0;
volatile uint32_t comp1_capture_time_discharge = 0;

// Internal capture state
volatile uint32_t capstate = 0;
volatile uint32_t last_count;
volatile uint32_t catch_counter;

// ============================================================================
// Metal Detector Functions
// ============================================================================

/**
 * @brief Arm TIM3 input capture for comparator timing
 * Configures timer to capture rising edge (charge time)
 */
void TIM3_arm_capture(void)
{
  // Set up triggers for TIM3 for input capture
  TIM_MasterConfigTypeDef sMasterConfig = {0};
  capstate = 0;
  sMasterConfig.MasterOutputTrigger = TIM_TRGO_ENABLE;
  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
  if (HAL_TIMEx_MasterConfigSynchronization(&htim2, &sMasterConfig) != HAL_OK)
  {
    Error_Handler();
  }
  __HAL_TIM_SET_COUNTER(&htim3, 0);

  HAL_TIM_IC_Start_IT(&htim3, TIM_CHANNEL_1); // Enable IC + interrupt
  HAL_TIM_Base_Start(&htim3);                 // Start timer counting
}

/**
 * @brief Trigger a single pulse on TIM2 Channel 1 with specified width
 * @param pulse_width_us Pulse width in microseconds
 * @note This generates a one-shot pulse. The pin will go HIGH for the specified duration.
 * @note Make sure TIM2_CH1 is configured on your desired output pin (typically PA0, PA5, or PA15)
 * Also- sets up the Input catpure stuff as well!
 */
void TIM2_Trigger_Pulse(uint32_t pulse_width_us)
{
  /* Stop timer if it's running */
  htim2.Instance->CR1 &= ~TIM_CR1_CEN; // Clear counter enable bit
  capstate = 0;
  TIM3_arm_capture();

  /* Reset counter to start from 0 */
  __HAL_TIM_SET_COUNTER(&htim2, 0);

  /* Set the period to match pulse width (for one-shot operation) */
  __HAL_TIM_SET_AUTORELOAD(&htim2, pulse_width_us + 1);

  /* Set the pulse width (duty cycle = 100% of period for full pulse) */
  __HAL_TIM_SET_COMPARE(&htim2, TIM_CHANNEL_1, 1);
  __HAL_TIM_SET_COMPARE(&htim2, TIM_CHANNEL_4, 1);

  /* Configure for one-pulse mode (timer stops after one period) */
  htim2.Instance->CR1 |= TIM_CR1_OPM;

  /* Enable outputs on first call only (they stay enabled after that) */
  htim2.Instance->CCER |= (TIM_CCER_CC1E | TIM_CCER_CC4E);

  /* Start the counter */
  htim2.Instance->CR1 |= TIM_CR1_CEN;
}

/**
 * @brief TIM3 Input Capture Callback - captures charge and discharge times
 * Called automatically when input capture event occurs
 * @param htim Timer handle
 */
void HAL_TIM_IC_CaptureCallback(TIM_HandleTypeDef *htim)
{
  if (htim->Instance == TIM3)
  {
    if (htim->Channel == HAL_TIM_ACTIVE_CHANNEL_1) // COMP1
    {
      // Set up triggers for TIM3 for input capture
      TIM_MasterConfigTypeDef sMasterConfig = {0};
      TIM_IC_InitTypeDef sConfigIC = {0};

      if (capstate == 0)
      {
        // First capture: rising edge (charge time)- how long from HBridge switch until edge
        comp1_capture_time_charge = HAL_TIM_ReadCapturedValue(&htim3, TIM_CHANNEL_1);

        sMasterConfig.MasterOutputTrigger = TIM_TRGO_UPDATE;
        sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
        if (HAL_TIMEx_MasterConfigSynchronization(&htim2, &sMasterConfig) != HAL_OK)
        {
          Error_Handler();
        }

        // Now set it up for falling edge for the discharge cycle
        sConfigIC.ICPolarity = TIM_INPUTCHANNELPOLARITY_FALLING;
        sConfigIC.ICSelection = TIM_ICSELECTION_DIRECTTI;
        sConfigIC.ICPrescaler = TIM_ICPSC_DIV1;
        sConfigIC.ICFilter = 0;
        if (HAL_TIM_IC_ConfigChannel(&htim3, &sConfigIC, TIM_CHANNEL_1) != HAL_OK)
        {
          Error_Handler();
        }
        htim3.Instance->CR1 &= ~TIM_CR1_CEN; // Clear counter enable bit
        __HAL_TIM_SET_COUNTER(&htim3, 0);    // and reset it to zero for the next timing
        // This is now the time to trigger the metal detector pulse
        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_SET);    // Debug pin high
        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_RESET);  // Debug pin low

      }
      else
      {
        // Second capture: falling edge (discharge time)
        comp1_capture_time_discharge = HAL_TIM_ReadCapturedValue(&htim3, TIM_CHANNEL_1);
        HAL_TIM_Base_Stop(&htim3);                 // Stop timer counting
        HAL_TIM_IC_Stop_IT(&htim3, TIM_CHANNEL_1); // Disable IC + interrupt

        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_SET);    // Debug pin high
        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_RESET);  // Debug pin low
        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_SET);    // Debug pin high
        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_RESET);  // Debug pin low

        // Now set it up for rising edge for next scan
        sConfigIC.ICPolarity = TIM_INPUTCHANNELPOLARITY_RISING;
        sConfigIC.ICSelection = TIM_ICSELECTION_DIRECTTI;
        sConfigIC.ICPrescaler = TIM_ICPSC_DIV1;
        sConfigIC.ICFilter = 0;
        if (HAL_TIM_IC_ConfigChannel(&htim3, &sConfigIC, TIM_CHANNEL_1) != HAL_OK)
        {
          Error_Handler();
        }
      }
      capstate += 1;

      comp1_captured = 1;
    }

    if (htim->Channel == HAL_TIM_ACTIVE_CHANNEL_2) // COMP2
    {
      uint32_t comp2_time = HAL_TIM_ReadCapturedValue(&htim3, TIM_CHANNEL_2);
      // Handle COMP2 capture
    }
  }
}
