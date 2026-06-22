/*
 * metal_detector.c - PI metal detector sequencer
 *
 * Cycle timeline (all times from md_start_cycle CEN↑, PSC=199 → 1 µs/tick):
 *
 *   t = 0
 *     Foreground (md_start_cycle): Force Active on TIM5 CH1 → PA0 HIGH.
 *     Arm "Inactive on compare" at CCR1 = MD_TX_PULSE_US. Start TIM5.
 *
 *   t = MD_TX_PULSE_US  [default 120 µs]
 *     Hardware OC fires: PA0 LOW (TX coil off). TIM5 CC1 interrupt.
 *     ISR 1 (md_tim5_cc1_fired):
 *       Stop TIM5.
 *       Load TIM1: CCR2=CCR3=CCR4=md_blanking_us, ARR=md_blanking_us+md_rx_window_us.
 *       Set all three PE channels to Active-on-compare. Start TIM1 (OPM).
 *
 *   t = MD_TX_PULSE_US + md_blanking_us  [default 120+16 = 136 µs]
 *     TIM1 hardware compare fires on CH2, CH3, CH4 simultaneously (same CCR):
 *       PE11 (U4B) HIGH — restores gain after TX ringdown
 *       PE13 (U4C) HIGH — enables integrator (RX charge accumulation begins)
 *       PE14 (U4D) LOW  — connects RX path (CC4P=1 inverts OC4REF)
 *
 *   t = MD_TX_PULSE_US + md_blanking_us + md_rx_window_us  [default 136+3 = 139 µs]
 *     TIM1 OPM expires (ARR reached). TIM1 UIE interrupt.
 *     ISR 2 (md_tim1_uie_fired):
 *       Force Inactive on CH4 → PE14 HIGH (RX path disconnects).
 *       PE13 (U4C) stays HIGH — integrator holds sampled charge.
 *       Start ADC conversion in interrupt mode.
 *
 *   t ≈ 139 µs + ~1 µs ADC conversion time
 *     ADC EOC interrupt.
 *     ISR 3 (md_adc_eoc_fired):
 *       Force Inactive on CH2 → PE11 LOW (gain-protect re-asserted).
 *       Force Inactive on CH3 → PE13 LOW (integrator reset, ready for next cycle).
 *       Read ADC result. Set md_adc_result_ready flag.
 *
 * Key property: because CH2, CH3, and CH4 share an identical CCR value, the
 * Active-on-compare compare event fires all three in the same hardware clock
 * cycle. The leading edges of U4B, U4C, and U4D are therefore aligned to
 * within one timer clock (1 µs) with no ISR jitter on any edge.
 *
 * APB1/APB2 timer clocks = 200 MHz (SYSCLK=400 MHz, AHB÷2, APBx÷2).
 * TIM5 on APB1, TIM1 on APB2; both use PSC=199.
 */

#include "metal_detector.h"
#include "main.h"
#include <stdio.h>
#include <string.h>

extern TIM_HandleTypeDef htim1;
extern TIM_HandleTypeDef htim5;
extern ADC_HandleTypeDef hadc1;

volatile uint8_t  md_adc_result_ready = 0;
volatile uint32_t md_adc_result       = 0;
volatile uint32_t md_blanking_us      = MD_BLANKING_US;
volatile uint32_t md_rx_window_us     = MD_RX_WINDOW_US;

volatile uint32_t md_ts_cen;
volatile uint32_t md_ts_tim5;
volatile uint32_t md_ts_tim1;

static inline uint32_t dwt_cyccnt(void)
{
    if (!(CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk))
        CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    if (!(DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk))
        DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    return DWT->CYCCNT;
}

/* ---------------------------------------------------------------------------
 * TIM5 CCMR1 — CH1 (PA0 / TX coil)
 *   OC1M at bits [6:4], extended bit at [16]
 * ------------------------------------------------------------------------- */
#define CCMR1_OC1M_MASK          (TIM_CCMR1_OC1M | (1U << 16))
#define CCMR1_OC1M_FORCE_INACT   (4U << 4)    /* OCxREF=0 immediately             */
#define CCMR1_OC1M_FORCE_ACT     (5U << 4)    /* OCxREF=1 immediately             */
#define CCMR1_OC1M_INACT_ON_CMP  (2U << 4)    /* OCxREF→0 when CNT=CCR1           */

/* ---------------------------------------------------------------------------
 * TIM1 CCMR1 — CH2 (PE11 / U4B, gain protect)
 *   OC2M at bits [14:12], extended bit at [24]
 * ------------------------------------------------------------------------- */
#define CCMR1_OC2M_MASK          ((7U << 12) | (1U << 24))
#define CCMR1_OC2M_FORCE_INACT   (4U << 12)   /* OC2REF=0 → PE11 LOW              */
#define CCMR1_OC2M_ACT_ON_CMP   (1U << 12)    /* OC2REF→1 when CNT=CCR2           */

/* ---------------------------------------------------------------------------
 * TIM1 CCMR2 — CH3 (PE13 / U4C, integrator enable)
 *   OC3M at bits [6:4], extended bit at [16]
 * ------------------------------------------------------------------------- */
#define CCMR2_OC3M_MASK          ((7U << 4) | (1U << 16))
#define CCMR2_OC3M_FORCE_INACT   (4U << 4)    /* OC3REF=0 → PE13 LOW              */
#define CCMR2_OC3M_ACT_ON_CMP   (1U << 4)     /* OC3REF→1 when CNT=CCR3           */

/* ---------------------------------------------------------------------------
 * TIM1 CCMR2 — CH4 (PE14 / U4D, RX path connect; CC4P=1 inverts pin sense)
 *   OC4M at bits [14:12], extended bit at [24]
 *   Idle  (Force Inactive): OC4REF=0 → CC4P=1 → PE14=HIGH (RX disconnected)
 *   Active (compare fires): OC4REF=1 → CC4P=1 → PE14=LOW  (RX connected)
 * ------------------------------------------------------------------------- */
#define CCMR2_OC4M_MASK          ((7U << 12) | (1U << 24))
#define CCMR2_OC4M_FORCE_INACT   (4U << 12)   /* OC4REF=0 → PE14 HIGH (idle)      */
#define CCMR2_OC4M_ACT_ON_CMP   (1U << 12)    /* OC4REF→1 when CNT=CCR4 → PE14 LOW */


/* ============================================================================
 * MD_Hardware_Init
 * Called once from main() after all MX_ inits. Configures timers and ADC
 * for the 3-ISR chain; leaves everything in a safe idle state.
 * ========================================================================== */
void MD_Hardware_Init(void)
{
    MD_Load_Settings();   /* apply saved blanking/window before first cycle   */

    /* ------------------------------------------------------------------
     * TIM5 — PA0 (CH1) TX coil pulse via "Inactive on compare" OC mode.
     *
     * Not OPM: timer runs freely after CC1 fires so ISR1 can observe CNT
     * if needed; ISR1 stops it with a manual CEN clear.
     * ARR = max (32-bit) so the counter never wraps between cycles.
     * CCR1 = MD_TX_PULSE_US — the hardware compare that ends PA0 and
     * simultaneously triggers the CC1IE interrupt (ISR 1).
     * ------------------------------------------------------------------ */
    TIM5->CR1  &= ~TIM_CR1_OPM;         /* run past CC1; ISR1 stops manually    */
    TIM5->ARR   = 0xFFFFFFFFU;           /* never roll over                       */
    TIM5->CCR1  = MD_TX_PULSE_US;        /* PA0-end reference; ISR1 fires here    */

    /* Establish OC1REF=LOW now (Force Inactive), then arm the compare mode.
     * md_start_cycle switches to Force Active before each TX pulse. */
    TIM5->CCMR1 = (TIM5->CCMR1 & ~CCMR1_OC1M_MASK) | CCMR1_OC1M_FORCE_INACT;
    TIM5->CCMR1 = (TIM5->CCMR1 & ~CCMR1_OC1M_MASK) | CCMR1_OC1M_INACT_ON_CMP;
    TIM5->CCER |= TIM_CCER_CC1E;         /* enable CH1 OC output to PA0           */

    TIM5->EGR   = TIM_EGR_UG;           /* latch preload values                  */
    __HAL_TIM_CLEAR_FLAG(&htim5, TIM_FLAG_UPDATE | TIM_FLAG_CC1);
    TIM5->DIER  = TIM_DIER_CC1IE;        /* only CC1IE needed; UIE not used       */
    HAL_NVIC_SetPriority(TIM5_IRQn, 5, 0);
    HAL_NVIC_EnableIRQ(TIM5_IRQn);

    /* ------------------------------------------------------------------
     * TIM1 — OPM (set by CubeMX). Drives PE11/PE13/PE14 via OC outputs.
     * PE11 stays as TIM1 CH2 AF from MspPostInit (no GPIO reconfigure).
     *
     * All three channels start in Force Inactive so pins are in their
     * correct idle states before the first cycle:
     *   PE11 (U4B, CC2P=0): OC2REF=LOW → PE11=LOW   (gain-protect on)
     *   PE13 (U4C, CC3P=0): OC3REF=LOW → PE13=LOW   (integrator reset)
     *   PE14 (U4D, CC4P=1): OC4REF=LOW → PE14=HIGH  (RX path off)
     *
     * ISR1 will switch all three to Active-on-compare with identical CCR
     * values each cycle; MOE=1 is left on permanently so OC outputs drive
     * pins directly — no MOE toggling required.
     * ------------------------------------------------------------------ */
    TIM1->SMCR &= ~(TIM_SMCR_SMS | TIM_SMCR_TS);  /* no hardware slave trigger  */

    TIM1->CCMR1 = (TIM1->CCMR1 & ~CCMR1_OC2M_MASK) | CCMR1_OC2M_FORCE_INACT;
    TIM1->CCMR2 = CCMR2_OC3M_FORCE_INACT | CCMR2_OC4M_FORCE_INACT;

    TIM1->CCER |= TIM_CCER_CC2E | TIM_CCER_CC3E | TIM_CCER_CC4E | TIM_CCER_CC4P;
    TIM1->CR2  &= ~(TIM_CR2_OIS2 | TIM_CR2_OIS3 | TIM_CR2_OIS4);

    /* MOE=1 always. OSSR/OSSI kept in case MOE is ever accidentally cleared. */
    TIM1->BDTR |= TIM_BDTR_MOE | TIM_BDTR_OSSR | TIM_BDTR_OSSI;

    TIM1->EGR   = TIM_EGR_UG;
    __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE);
    TIM1->DIER  = TIM_DIER_UIE;          /* UIE fires ISR2 when OPM expires       */
    HAL_NVIC_SetPriority(TIM1_UP_IRQn, 5, 0);
    HAL_NVIC_EnableIRQ(TIM1_UP_IRQn);

    HAL_ADCEx_Calibration_Start(&hadc1, ADC_CALIB_OFFSET, ADC_SINGLE_ENDED);

    /* 16× hardware oversampling: accumulate 16 conversions per trigger, right-shift
     * by 4 to normalize back to the 16-bit output range.  A single HAL_ADC_Start_IT
     * still triggers the whole burst and fires one EOC — ISR3 is unchanged. */
    hadc1.Init.OversamplingMode                   = ENABLE;
    hadc1.Init.Oversampling.Ratio                 = 16UL;
    hadc1.Init.Oversampling.RightBitShift         = ADC_RIGHTBITSHIFT_4;
    hadc1.Init.Oversampling.TriggeredMode         = ADC_TRIGGEREDMODE_SINGLE_TRIGGER;
    hadc1.Init.Oversampling.OversamplingStopReset = ADC_REGOVERSAMPLING_CONTINUED_MODE;
    HAL_ADC_Init(&hadc1);

    HAL_NVIC_SetPriority(ADC_IRQn, 5, 0);
    HAL_NVIC_EnableIRQ(ADC_IRQn);        /* ADC EOC fires ISR3                    */
}

/* ============================================================================
 * md_start_cycle  (foreground — called from FreeRTOS task)
 *
 * Arms the TX pulse on PA0 and starts TIM5. Returns immediately; the rest of
 * the cycle is driven by the ISR chain. No-op if TIM5 is still running
 * (guards against re-entry before the previous cycle completes).
 * ========================================================================== */
void md_start_cycle(void)
{
    if (TIM5->CR1 & TIM_CR1_CEN)
        return;

    /* Two writes to CCMR1: first Force Active drives PA0=HIGH this instant;
     * second arms Inactive-on-compare so the hardware pulls PA0=LOW at
     * CNT=CCR1=MD_TX_PULSE_US — no ISR involvement on the falling edge. */
    TIM5->CCMR1 = (TIM5->CCMR1 & ~CCMR1_OC1M_MASK) | CCMR1_OC1M_FORCE_ACT;
    TIM5->CCMR1 = (TIM5->CCMR1 & ~CCMR1_OC1M_MASK) | CCMR1_OC1M_INACT_ON_CMP;
    TIM5->CNT   = 0U;
    __HAL_TIM_CLEAR_FLAG(&htim5, TIM_FLAG_CC1);
    md_ts_cen   = dwt_cyccnt();
    TIM5->CR1  |= TIM_CR1_CEN;          /* TX pulse begins here                  */
}

/* ============================================================================
 * ISR 1 — md_tim5_cc1_fired  (TIM5 CC1 compare match)
 *
 * Fires when TIM5 CNT reaches CCR1 = MD_TX_PULSE_US. At this moment the
 * hardware OC has already pulled PA0 LOW (TX coil off). This ISR:
 *   1. Stops TIM5 (no longer needed this cycle).
 *   2. Loads TIM1 with blanking + RX window timing.
 *   3. Sets all three PE channels to Active-on-compare with equal CCR so
 *      their leading edges fire in the same hardware clock cycle.
 *   4. Starts TIM1 (OPM). The PE signals will rise automatically at CNT=bl.
 *
 * Called from HAL_TIM_OC_DelayElapsedCallback (main.c) when htim == TIM5.
 * ========================================================================== */
void md_tim5_cc1_fired(void)
{
    md_ts_tim5 = dwt_cyccnt();

    TIM5->CR1 &= ~TIM_CR1_CEN;          /* stop TIM5; its role in this cycle is done */

    /* Force Inactive on all PE channels before switching modes.  This ensures
     * OC refs are LOW (pins at idle) regardless of how the previous cycle ended,
     * so there is no spurious edge when Active-on-compare is then armed. */
    TIM1->CCMR1 = (TIM1->CCMR1 & ~CCMR1_OC2M_MASK) | CCMR1_OC2M_FORCE_INACT;
    TIM1->CCMR2 = CCMR2_OC3M_FORCE_INACT | CCMR2_OC4M_FORCE_INACT;

    /* Snapshot runtime-tunable values once so ISR2/ISR3 see consistent timing. */
    uint32_t bl = md_blanking_us;
    uint32_t rx = md_rx_window_us;

    /* TIM1 ARR = end of RX window (OPM fires ISR2 here).
     * CCR2=CCR3=CCR4=bl: identical compare value → all three OC events fire
     * in the same hardware clock → U4B, U4C, U4D leading edges are aligned. */
    TIM1->ARR  = (uint16_t)(bl + rx);
    TIM1->CCR2 = (uint16_t)bl;          /* PE11 (U4B) rises here                 */
    TIM1->CCR3 = (uint16_t)bl;          /* PE13 (U4C) rises here                 */
    TIM1->CCR4 = (uint16_t)bl;          /* PE14 (U4D) falls here (CC4P inverts)  */

    /* Arm Active-on-compare. OC refs remain LOW (from Force Inactive above)
     * until CNT=bl triggers all three simultaneously. */
    TIM1->CCMR1 = (TIM1->CCMR1 & ~CCMR1_OC2M_MASK) | CCMR1_OC2M_ACT_ON_CMP;
    TIM1->CCMR2 = CCMR2_OC3M_ACT_ON_CMP | CCMR2_OC4M_ACT_ON_CMP;

    TIM1->CNT = 0U;
    __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE | TIM_FLAG_CC2 | TIM_FLAG_CC3 | TIM_FLAG_CC4);
    TIM1->CR1 |= TIM_CR1_CEN;           /* blanking + RX window begins           */
}

/* ============================================================================
 * ISR 2 — md_tim1_uie_fired  (TIM1 UIE / OPM expiry)
 *
 * Fires when TIM1 CNT reaches ARR = md_blanking_us + md_rx_window_us.
 * The RX window is now closed:
 *   - Disconnect RX path (PE14/U4D → HIGH) by forcing CH4 inactive.
 *   - Leave PE13/U4C HIGH: integrator still holds the sampled charge; the
 *     ADC must read before U4C is released.
 *   - Start ADC conversion in interrupt mode (result delivered to ISR3).
 *
 * Called from HAL_TIM_PeriodElapsedCallback (main.c) when htim == TIM1.
 * ========================================================================== */
void md_tim1_uie_fired(void)
{
    md_ts_tim1 = dwt_cyccnt();

    /* Close RX path first — CH4 Force Inactive: OC4REF=LOW, CC4P=1 → PE14=HIGH. */
    TIM1->CCMR2 = (TIM1->CCMR2 & ~CCMR2_OC4M_MASK) | CCMR2_OC4M_FORCE_INACT;

    /* U4C (PE13) intentionally stays HIGH here. The integrator capacitor holds
     * the charge sampled during the RX window; releasing U4C before the ADC
     * reads would reset it and corrupt the result. ISR3 releases U4C after
     * HAL_ADC_GetValue returns. */
    HAL_ADC_Start_IT(&hadc1);
}

/* ============================================================================
 * ISR 3 — md_adc_eoc_fired  (ADC end-of-conversion)
 *
 * Fires when the ADC finishes converting the RX sample. Sequence:
 *   1. Release PE11/U4B (gain-protect) and PE13/U4C (integrator) — the ADC
 *      data register is already latched so deactivating U4C is safe here.
 *   2. Read the result from the ADC data register.
 *   3. Set md_adc_result_ready so the foreground task can call md_get_result.
 *
 * Called from HAL_ADC_ConvCpltCallback (main.c) when hadc == ADC1.
 * ========================================================================== */
void md_adc_eoc_fired(void)
{
    /* Release gain-protect (CH2/PE11) and integrator (CH3/PE13).
     * OC refs → LOW; with CC2P=CC3P=0 both pins go LOW immediately. */
    TIM1->CCMR1 = (TIM1->CCMR1 & ~CCMR1_OC2M_MASK) | CCMR1_OC2M_FORCE_INACT;
    TIM1->CCMR2 = (TIM1->CCMR2 & ~CCMR2_OC3M_MASK) | CCMR2_OC3M_FORCE_INACT;

    md_adc_result = HAL_ADC_GetValue(&hadc1);  /* DR already latched at EOC      */
    HAL_ADC_Stop_IT(&hadc1);                   /* disable EOCIE; reset HAL state  */

    md_adc_result_ready = 1;
}

/* ============================================================================
 * md_get_result  (foreground)
 * ========================================================================== */
uint8_t md_get_result(uint32_t *result)
{
    if (!md_adc_result_ready)
        return 0;
    *result             = md_adc_result;
    md_adc_result_ready = 0;
    return 1;
}

/* ============================================================================
 * Flash settings storage
 *
 * Sector 7 (0x080E0000, 128KB) holds one 32-byte flash word containing the
 * runtime timing parameters.  The struct is exactly 32 bytes so it satisfies
 * the H7 flash word-write alignment requirement.
 * ========================================================================== */
typedef struct {
    uint32_t magic;         /* MD_SETTINGS_MAGIC when valid                   */
    uint32_t blanking_us;
    uint32_t rx_window_us;
    uint8_t  _pad[20];      /* pad to 32 bytes (H7 flash word = 256 bits)     */
} MD_Settings_t;

void MD_Load_Settings(void)
{
    const MD_Settings_t *s = (const MD_Settings_t *)MD_SETTINGS_FLASH_ADDR;
    if (s->magic == MD_SETTINGS_MAGIC) {
        md_blanking_us  = s->blanking_us;
        md_rx_window_us = s->rx_window_us;
        printf("# settings loaded: blanking=%luus  rx_window=%luus\r\n",
               md_blanking_us, md_rx_window_us);
    } else {
        printf("# no saved settings -- defaults: blanking=%luus  rx_window=%luus\r\n",
               md_blanking_us, md_rx_window_us);
    }
}

void MD_Save_Settings(void)
{
    /* Source buffer must be in RAM and 32-byte aligned for FLASHWORD program. */
    static MD_Settings_t __attribute__((aligned(32))) buf;
    buf.magic        = MD_SETTINGS_MAGIC;
    buf.blanking_us  = md_blanking_us;
    buf.rx_window_us = md_rx_window_us;
    memset(buf._pad, 0xFF, sizeof(buf._pad));

    /* MPU region 1 covers the entire flash as PRIV_RO.  HAL_FLASH_Program
     * writes directly to the memory-mapped flash address, so the MPU fires
     * MemManage before the flash controller sees the write.  Disable MPU
     * for the erase+program sequence and restore immediately after. */
    HAL_MPU_Disable();
    HAL_FLASH_Unlock();

    FLASH_EraseInitTypeDef erase = {
        .TypeErase    = FLASH_TYPEERASE_SECTORS,
        .Banks        = FLASH_BANK_1,
        .Sector       = FLASH_SECTOR_7,
        .NbSectors    = 1,
        .VoltageRange = FLASH_VOLTAGE_RANGE_3,  /* 3.3V board, 32-bit writes  */
    };
    uint32_t err = 0;
    HAL_FLASHEx_Erase(&erase, &err);

    HAL_FLASH_Program(FLASH_TYPEPROGRAM_FLASHWORD,
                      MD_SETTINGS_FLASH_ADDR,
                      (uint32_t)&buf);

    HAL_FLASH_Lock();
    SCB_InvalidateICache();              /* flush stale I-cache entries for flash */
    HAL_MPU_Enable(MPU_PRIVILEGED_DEFAULT);

    printf("# saved: blanking=%luus  rx_window=%luus\r\n",
           md_blanking_us, md_rx_window_us);
}
