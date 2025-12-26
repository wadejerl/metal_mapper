/*
 * gps.h - GPS parsing and processing module
 * High-precision NMEA parser with DMA circular buffer
 */

#ifndef GPS_H
#define GPS_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "stm32h7xx_hal.h"
#include "minmea.h"

// ============================================================================
// GPS Configuration and Buffer
// ============================================================================
#define GPS_DMA_BUFFER_SIZE 512

// GPS circular buffer (32-byte aligned for D-Cache)
extern __attribute__((aligned(32))) uint8_t gps_dma_buffer[GPS_DMA_BUFFER_SIZE];
extern volatile uint32_t gps_buffer_read_pos;

// GPS position cache (high-precision for mm-level accuracy)
typedef struct {
  double latitude;   // Use double for up to 11 decimal places (~0.1mm precision)
  double longitude;  // Use double for up to 11 decimal places (~0.1mm precision)
  uint8_t fix_quality;
  uint8_t satellites;
  uint8_t valid;
  float timestamp;
} gps_position_t;

extern volatile gps_position_t latest_gps_position;
extern volatile uint8_t new_gga_available;

// Error recovery state
extern volatile uint32_t last_gga_timestamp;

// ============================================================================
// GPS Functions
// ============================================================================

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
 * @brief Convert minmea coordinate to double precision
 * @param f Fixed-point coordinate from minmea
 * @return Double precision coordinate in decimal degrees
 */
double minmea_tocoord_double(const struct minmea_float *f);

#ifdef __cplusplus
}
#endif

#endif /* GPS_H */
