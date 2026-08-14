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
                     // fix anchors (the frame paced to land just before it)
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

// Module identity from the $PQTMVERNO / $PQTMUNIQID replies during boot
// config (raw payload, checksum stripped; "" until answered). Reported
// by the 'I' info line so the daemon can stamp studies with them.
extern char gps_version_str[64];
extern char gps_uniqid_str[64];

// Error recovery state
extern volatile uint32_t last_gga_timestamp;

// ============================================================================
// GPS Functions
// ============================================================================

/**
 * @brief Send the module configuration commands on USART2
 * Enables GGA/GSV output and disables GSA/VTG/GLL/RMC (Quectel
 * $PQTMCFGMSGRATE). Call once at startup, after the GPS has had
 * time to boot. Blocking transmit.
 */
void gps_send_config(void);

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
