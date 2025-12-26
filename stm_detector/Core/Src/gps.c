/*
 * gps.c - GPS parsing and processing module
 * High-precision NMEA parser with DMA circular buffer
 */

#include "gps.h"
#include "metal_detector.h"
#include "main.h"
#include <string.h>
#include <math.h>
#include <stdio.h>

// External UART handle
extern UART_HandleTypeDef huart2;

// ============================================================================
// GPS Buffer and State Variables
// ============================================================================
__attribute__((aligned(32))) uint8_t gps_dma_buffer[GPS_DMA_BUFFER_SIZE];
volatile uint32_t gps_buffer_read_pos = 0;

volatile gps_position_t latest_gps_position = {0};
volatile uint8_t new_gga_available = 0;

// Error recovery state
volatile uint32_t last_gga_timestamp = 0;
static uint32_t last_dma_position = 0;
static uint32_t last_dma_check_time = 0;

// ============================================================================
// GPS Processing Functions
// ============================================================================

/**
 * @brief Process GPS data from DMA circular buffer (non-blocking)
 *
 * Robust GPS parser with automatic recovery from:
 * - UART errors (overrun, framing, noise from GPS power cycle)
 * - DMA stuck conditions (hardware lockup)
 * - Framing loss (parsing mid-sentence after reset)
 *
 * Extracts NMEA sentences and parses multiple types (GGA priority).
 */
void gps_process_dma_buffer(void)
{
  char sentence[MINMEA_MAX_SENTENCE_LENGTH + 1];
  int sentences_found = 0;
  uint32_t now = HAL_GetTick();
  uint32_t current_dma_pos = GPS_DMA_BUFFER_SIZE - __HAL_DMA_GET_COUNTER(huart2.hdmarx);

  // ========== UART Error Recovery ==========
  // Clear UART error flags (overrun/framing/noise from GPS disconnect/reconnect)
  // No need to restart - DMA continues, next valid sentence will parse correctly
  if (huart2.ErrorCode != HAL_UART_ERROR_NONE)
  {
    __HAL_UART_CLEAR_FLAG(&huart2, UART_CLEAR_OREF | UART_CLEAR_NEF | UART_CLEAR_PEF | UART_CLEAR_FEF);
    huart2.ErrorCode = HAL_UART_ERROR_NONE;
    // Continue processing - gps_find_nmea_sentence() will self-synchronize
  }

  // ========== DMA Stuck Detection (Last Resort) ==========
  // Only restart if DMA truly stuck for 5+ seconds (hardware failure)
  if (current_dma_pos != last_dma_position)
  {
    last_dma_position = current_dma_pos;
    last_dma_check_time = now;
  }
  else if (last_dma_check_time > 0 && (now - last_dma_check_time) > 5000)
  {
    printf("DMA HARDWARE STUCK - Last resort restart\n");
    HAL_UART_AbortReceive(&huart2);
    HAL_UART_DeInit(&huart2);
    HAL_UART_Init(&huart2);
    HAL_UART_Receive_DMA(&huart2, gps_dma_buffer, GPS_DMA_BUFFER_SIZE);
    // Re-enable character match interrupt after restart
    huart2.Instance->CR1 &= ~USART_CR1_UE;  // Disable to write CR2
    huart2.Instance->CR2 &= ~(0xFF << 24);
    huart2.Instance->CR2 |= ((uint32_t)'\n' << 24);
    huart2.Instance->CR1 |= USART_CR1_UE;   // Re-enable
    __HAL_UART_ENABLE_IT(&huart2, UART_IT_CM);
    gps_buffer_read_pos = 0;
    last_dma_position = 0xFFFFFFFF;
    last_dma_check_time = now;
    return;
  }

  // ========== Normal Sentence Processing ==========
  while (sentences_found < 10 && gps_find_nmea_sentence(sentence, MINMEA_MAX_SENTENCE_LENGTH))
  {
    sentences_found++;

    // Strip trailing whitespace
    int len = strlen(sentence);
    while (len > 0 && (sentence[len-1] == '\r' || sentence[len-1] == '\n'))
      sentence[--len] = '\0';

    // Parse GGA sentences (PRIORITY - checked first)
    if (strncmp(sentence, "$GPGGA", 6) == 0 || strncmp(sentence, "$GNGGA", 6) == 0)
    {
      gps_parse_gga_sentence(sentence);

      // re-enable another metal detector trigger from the UART interrupt
      //__HAL_UART_ENABLE_IT(&huart2, UART_IT_RXNE);

      last_gga_timestamp = HAL_GetTick();
    }
    // Parse RMC sentences (Recommended Minimum)
    else if (strncmp(sentence, "$GPRMC", 6) == 0 || strncmp(sentence, "$GNRMC", 6) == 0)
    {
      struct minmea_sentence_rmc frame;
      if (minmea_parse_rmc(&frame, sentence)) {
        // RMC parsed successfully - data available in frame if needed later
      }
    }
    // Parse GSV sentences (Satellites in View)
    else if (strncmp(sentence, "$GPGSV", 6) == 0 || strncmp(sentence, "$GNGSV", 6) == 0 ||
             strncmp(sentence, "$GLGSV", 6) == 0 || strncmp(sentence, "$GAGSV", 6) == 0)
    {
      struct minmea_sentence_gsv frame;
      if (minmea_parse_gsv(&frame, sentence)) {
        // GSV parsed successfully - satellite info available in frame if needed later
      }
    }
    // Parse GSA sentences (DOP and Active Satellites)
    else if (strncmp(sentence, "$GPGSA", 6) == 0 || strncmp(sentence, "$GNGSA", 6) == 0 ||
             strncmp(sentence, "$GLGSA", 6) == 0 || strncmp(sentence, "$GAGSA", 6) == 0)
    {
      struct minmea_sentence_gsa frame;
      if (minmea_parse_gsa(&frame, sentence)) {
        // GSA parsed successfully - DOP values available in frame if needed later
      }
    }
    // Parse VTG sentences (Track Made Good and Ground Speed)
    else if (strncmp(sentence, "$GPVTG", 6) == 0 || strncmp(sentence, "$GNVTG", 6) == 0)
    {
      struct minmea_sentence_vtg frame;
      if (minmea_parse_vtg(&frame, sentence)) {
        // VTG parsed successfully - speed/course data available in frame if needed later
      }
    }
    // Parse GLL sentences (Geographic Position - Lat/Lon)
    else if (strncmp(sentence, "$GPGLL", 6) == 0 || strncmp(sentence, "$GNGLL", 6) == 0)
    {
      struct minmea_sentence_gll frame;
      if (minmea_parse_gll(&frame, sentence)) {
        // GLL parsed successfully - position data available in frame if needed later
      }
    }
    // Note: HDT (Heading True) is not supported by minmea library
  }
}

/**
 * @brief Find and extract next complete NMEA sentence from DMA buffer
 * @param output Buffer to store extracted sentence
 * @param max_len Maximum length of output buffer
 * @return 1 if sentence found, 0 otherwise
 */
int gps_find_nmea_sentence(char *output, uint32_t max_len)
{
  SCB_InvalidateDCache_by_Addr((uint32_t *)gps_dma_buffer, GPS_DMA_BUFFER_SIZE);

  uint32_t dma_write_pos = GPS_DMA_BUFFER_SIZE - __HAL_DMA_GET_COUNTER(huart2.hdmarx);
  if (dma_write_pos == gps_buffer_read_pos)
    return 0; // No new data

  uint32_t read_pos = gps_buffer_read_pos;
  uint32_t idx = 0;
  uint8_t found_start = 0;

  // Search for complete sentence: $....\n
  while (read_pos != dma_write_pos)
  {
    uint8_t c = gps_dma_buffer[read_pos];
    read_pos = (read_pos + 1) % GPS_DMA_BUFFER_SIZE;

    if (c == '$')
    {
      if (!found_start)
      {
        // First $ - start building sentence
        found_start = 1;
        idx = 0;
        output[idx++] = c;
      }
      else
      {
        // Second $ while building - discard malformed sentence
        found_start = 0;
        idx = 0;
      }
    }
    else if (found_start)
    {
      if (idx < max_len)
        output[idx++] = c;

      if (c == '\n')
      {
        // Complete sentence found
        output[idx] = '\0';
        gps_buffer_read_pos = read_pos;
        return 1;
      }

      // Buffer overflow - discard this sentence
      if (idx >= max_len)
      {
        found_start = 0;
        idx = 0;
      }
    }
  }

  // No complete sentence - advance read position to avoid rescanning
  gps_buffer_read_pos = read_pos;
  return 0;
}

/**
 * @brief Convert minmea coordinate to double precision (for high-accuracy GPS)
 * @param f minmea_float coordinate in DDDMM.MMMMMMMMMM format (up to 11 decimal places)
 * @return Coordinate in decimal degrees with full double precision
 */
double minmea_tocoord_double(const struct minmea_float *f)
{
  if (f->scale == 0)
    return NAN;
  if (f->scale > (INT_LEAST64_MAX / 100))
    return NAN;
  if (f->scale < (INT_LEAST64_MIN / 100))
    return NAN;

  // Use double precision throughout for mm-level accuracy (11 decimal places)
  int_least64_t degrees = f->value / (f->scale * 100);
  int_least64_t minutes = f->value % (f->scale * 100);
  return (double)degrees + (double)minutes / (60.0 * (double)f->scale);
}

/**
 * @brief Parse GGA sentence and update position cache
 * @param sentence NMEA GGA sentence string
 */
void gps_parse_gga_sentence(const char *sentence)
{
  struct minmea_sentence_gga frame;

  if (minmea_parse_gga(&frame, sentence))
  {
    // Update cached position with high precision (9 decimal places)
    latest_gps_position.latitude = minmea_tocoord_double(&frame.latitude);
    latest_gps_position.longitude = minmea_tocoord_double(&frame.longitude);
    latest_gps_position.fix_quality = frame.fix_quality;
    latest_gps_position.satellites = frame.satellites_tracked;
    latest_gps_position.timestamp = frame.time.seconds + frame.time.microseconds / 1000000.0;

    // Check if position is valid (not NaN and has fix)
    if (!isnan(latest_gps_position.latitude) &&
        !isnan(latest_gps_position.longitude) &&
        frame.fix_quality > 0)
    {
      latest_gps_position.valid = 1;
      new_gga_available = 1;  // Signal new data available

      // This is now the time to trigger the metal detector pulse
      /*HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_SET);    // Debug pin high
      HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_RESET);  // Debug pin low
      HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_SET);    // Debug pin high
      HAL_GPIO_WritePin(GPIOB, GPIO_PIN_11, GPIO_PIN_RESET);  // Debug pin low
      */
     
      TIM2_Trigger_Pulse(20); // this one call should set up the IC, induce a pulse, and there will be 2 triggerings of the IC

      // Print every GGA received with timestamp to verify real-time updates (9 decimal places for cm precision)
      /*
      printf("# GGA: %02d:%02d:%06.3f %.9f,%.9f sats=%d fix=%d\n",
             frame.time.hours,
             frame.time.minutes,
             latest_gps_position.timestamp,
             latest_gps_position.latitude,
             latest_gps_position.longitude,
             latest_gps_position.satellites,
             latest_gps_position.fix_quality);
             */
    }
    else
    {
      latest_gps_position.valid = 0;
    }
  }
}
