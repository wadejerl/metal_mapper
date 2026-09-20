/*
 * gps.h - GPS parsing and processing module
 * High-precision NMEA parser with DMA circular buffer
 *
 * Ported from stm_detector2 (STM32H723). G474 (Cortex-M4) has no data
 * cache, so the H7's 32-byte buffer alignment and cache invalidation
 * are gone; everything else is unchanged.
 */

#ifndef GPS_H
#define GPS_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "stm32g4xx_hal.h"
#include "minmea.h"

// ============================================================================
// GPS Configuration and Buffer
// ============================================================================
/* Sized so the ring can absorb a flash page erase (~25 ms) during which
 * the CPU stalls and the parser can't drain: ~1.2 KB arrives at 460800
 * in that window, plus normal backlog — 2048 gives ~2x margin. */
#define GPS_DMA_BUFFER_SIZE 2048

// GPS circular buffer (filled by USART2 RX DMA in circular mode)
extern uint8_t gps_dma_buffer[GPS_DMA_BUFFER_SIZE];
extern volatile uint32_t gps_buffer_read_pos;

// GPS position cache (high-precision for mm-level accuracy)
typedef struct {
  double latitude;   // Use double for up to 11 decimal places (~0.1mm precision)
  double longitude;  // Use double for up to 11 decimal places (~0.1mm precision)
  uint8_t fix_quality;
  uint8_t satellites;
  uint8_t valid;
  float timestamp;
  uint32_t tick;     // md_tick_completed at GGA parse: the sample tick this
                     // fix anchors (the newest completed frame at parse
                     // time — within one sample interval before the fix)
} gps_position_t;

extern volatile gps_position_t latest_gps_position;
extern volatile uint8_t new_gga_available;

// Dual-antenna true heading cache ($--THS). valid is set only while the
// latest THS carried status 'A'; the module reports 'V' whenever either
// antenna loses signal. last_ths_timestamp is the HAL_GetTick() of the
// last valid heading so consumers can also blank on stale data.
extern volatile float gps_heading_deg;
extern volatile uint8_t gps_heading_valid;
extern volatile uint32_t last_ths_timestamp;

// Startup diagnostics: while set (default at power-up), every received
// NMEA sentence is echoed to the console with a leading "# " so the CSV
// parser ignores it. Cleared by the app once configuration is done.
extern volatile uint8_t gps_echo_enabled;

// Steady-state passthrough of PQTMTXT/PQTMTAR/THS sentences as # lines.
// Human-debugging aid, off by default; toggled with the 'G' console key.
// Does not affect THS heading parsing, which always runs.
extern volatile uint8_t gps_passthrough_enabled;

// Module identity from the $PQTMVERNO / $PQTMUNIQID replies (raw payload,
// checksum stripped; "" until answered). Queried at boot config and again
// by gps_query_identity when the 'I' key finds them empty — the module
// powers up after the MCU and can miss the boot query. Reported by the
// 'I' info line so the daemon can stamp studies with them.
extern char gps_version_str[64];
extern char gps_uniqid_str[64];

// Error recovery state
extern volatile uint32_t last_gga_timestamp;

// ============================================================================
// GPS Functions
// ============================================================================

/**
 * @brief Send the module configuration burst on USART2 (boot; blocking)
 * Enables GGA output and disables GSV/GSA/VTG/GLL/RMC (Quectel
 * $PQTMCFGMSGRATE; GSV off since 2026-09-20 — see gps.c), saves and
 * restarts the module. ~130 ms of HAL_Delay plus up to 200 ms waiting for
 * the PQTMSAVEPAR ack before the restart (the full 200 ms when the GPS is
 * off): before MD_Hardware_Init only.
 */
void gps_send_config(void);

/**
 * @brief Re-send the configuration burst if the module missed it
 * Task loop, once per iteration. The module powers up after the MCU and
 * can miss the boot burst, or catch only its tail; a burst counts as
 * delivered only when every PQTMCFG command in it was answered. Once the
 * module has been heard, an undelivered burst goes out again — up to three
 * attempts, ~0.5 s after first heard and then after growing gaps — at most
 * one command per call, 10 ms apart, so nothing blocks beyond ~0.7 ms of
 * TX. Never waits for a module that says nothing.
 */
void gps_config_poll(void);

/**
 * @brief Delivery state of the config burst, for the 'I' info line
 * "boot" — every config command answered from the boot burst; "resent" —
 * answered in full after a re-send (it came up late); "pending" — heard,
 * re-sends under way (settles within ~10 s); "noreply" — heard, re-sends
 * spent, still not fully answered (may run stale settings); "silent" —
 * never heard (off, unplugged, or not emitting NMEA).
 */
const char *gps_config_state(void);

/**
 * @brief Re-send the $PQTMVERNO / $PQTMUNIQID identity queries
 * For the 'I' key when the boot-time query went unanswered (module not yet
 * up). Blocking transmit of two short sentences; replies are captured in
 * the USART2 ISR, so the caller polls gps_identity_known() afterwards.
 */
void gps_query_identity(void);

/**
 * @brief 1 once both gps_version_str and gps_uniqid_str are filled
 */
int gps_identity_known(void);

/**
 * @brief Process GPS data from DMA circular buffer (non-blocking)
 * Called from LF character match interrupt
 */
void gps_process_dma_buffer(void);

/**
 * @brief Find and extract next complete NMEA sentence from DMA buffer
 * @param output Buffer to store extracted sentence
 * @param max_len Maximum length of output buffer
 * @return 1 if sentence found, 0 otherwise
 */
int gps_find_nmea_sentence(char *output, uint32_t max_len);

/**
 * @brief Parse GGA sentence and update position cache
 * @param sentence NMEA sentence string
 */
void gps_parse_gga_sentence(const char *sentence);

/**
 * @brief Parse THS sentence (true heading) and update the heading cache
 * @param sentence NMEA sentence string, e.g. "$GNTHS,123.45,A*XX"
 */
void gps_parse_ths_sentence(const char *sentence);

/**
 * @brief Print queued passthrough sentences (# lines) to the console
 *
 * Passthrough sentences ($PQTMTXT/$PQTMTAR/$--THS) are queued by the
 * USART2 IRQ rather than printed there, so they cannot interleave into
 * a CSV line the task is assembling. Call once per task-loop iteration;
 * task context only.
 */
void gps_echo_drain(void);

/**
 * @brief Convert minmea coordinate to double precision
 * @param f Fixed-point coordinate from minmea
 * @return Double precision coordinate in decimal degrees
 */
double minmea_tocoord_double(const struct minmea_float *f);

#ifdef __cplusplus
}
#endif

#endif /* GPS_H */
