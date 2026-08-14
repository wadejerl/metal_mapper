/*
 * gps.c - GPS parsing and processing module
 * High-precision NMEA parser with DMA circular buffer
 *
 * Ported from stm_detector2 (STM32H723) to STM32G474: the M4 has no
 * D-cache, so the SCB_InvalidateDCache_by_Addr call and the 32-byte
 * buffer alignment were removed. All parsing logic and the four error
 * recovery mechanisms are unchanged.
 */

#include "gps.h"
#include "main.h"
#include "metal_detector.h"
#include <string.h>
#include <math.h>
#include <stdio.h>

// External UART handle
extern UART_HandleTypeDef huart2;

// ============================================================================
// GPS Buffer and State Variables
// ============================================================================
uint8_t gps_dma_buffer[GPS_DMA_BUFFER_SIZE];
volatile uint32_t gps_buffer_read_pos = 0;

volatile gps_position_t latest_gps_position = {0};
volatile uint8_t new_gga_available = 0;

// Dual-antenna true heading cache ($--THS). Valid only while the most
// recent THS carried status 'A' (autonomous); the module sends 'V' when
// either antenna loses signal, which clears it. The timestamp lets the
// reporter blank the field if THS output stops arriving entirely.
volatile float gps_heading_deg = 0.0f;
volatile uint8_t gps_heading_valid = 0;
volatile uint32_t last_ths_timestamp = 0;

// Module identity, captured from the replies to the $PQTMVERNO and
// $PQTMUNIQID queries sent during gps_send_config. Raw payload (after
// the sentence name, checksum stripped); empty string until the module
// answers. Written from the USART2 IRQ during boot, read by the 'I'
// info report long after — no concurrent access in practice.
char gps_version_str[64] = "";
char gps_uniqid_str[64] = "";

// ============================================================================
// Steady-state console passthrough queue
// ============================================================================
// Passthrough sentences (# lines) are queued here by the USART2 IRQ and
// printed from task context by gps_echo_drain(). Printing directly from
// the IRQ would interleave the # line into a CSV line the task is midway
// through assembling (both go to USART1), splitting one data record into
// two unparseable ones. Single-writer (IRQ) / single-reader (task) ring
// with free-running indices; complete lines only — a line that does not
// fit is dropped whole (these are diagnostics, not data).
#define GPS_ECHO_RING_SIZE 1024U            /* power of two */
static char gps_echo_ring[GPS_ECHO_RING_SIZE];
static volatile uint32_t gps_echo_head = 0; /* IRQ writes  */
static volatile uint32_t gps_echo_tail = 0; /* task writes */

static void gps_echo_enqueue(const char *sentence)
{
  static const char prefix[2] = { '#', ' ' };
  static const char crlf[2]   = { '\r', '\n' };
  uint32_t len  = strlen(sentence);
  uint32_t need = len + sizeof(prefix) + sizeof(crlf);
  uint32_t head = gps_echo_head;

  if (need > GPS_ECHO_RING_SIZE - (head - gps_echo_tail))
    return;                                 /* full: drop the whole line */

  for (uint32_t i = 0; i < sizeof(prefix); i++)
    gps_echo_ring[(head++) & (GPS_ECHO_RING_SIZE - 1U)] = prefix[i];
  for (uint32_t i = 0; i < len; i++)
    gps_echo_ring[(head++) & (GPS_ECHO_RING_SIZE - 1U)] = sentence[i];
  for (uint32_t i = 0; i < sizeof(crlf); i++)
    gps_echo_ring[(head++) & (GPS_ECHO_RING_SIZE - 1U)] = crlf[i];

  gps_echo_head = head;                     /* publish after the payload */
}

void gps_echo_drain(void)
{
  uint32_t head = gps_echo_head;  /* snapshot: always whole lines, and the
                                     last byte is '\n' so stdout's line
                                     buffer flushes everything we write */
  uint32_t tail = gps_echo_tail;

  while (tail != head)
  {
    uint32_t idx   = tail & (GPS_ECHO_RING_SIZE - 1U);
    uint32_t chunk = head - tail;
    if (chunk > GPS_ECHO_RING_SIZE - idx)   /* clip at the wrap point */
      chunk = GPS_ECHO_RING_SIZE - idx;
    fwrite(&gps_echo_ring[idx], 1, chunk, stdout);
    tail += chunk;
  }
  gps_echo_tail = tail;
}

// Echo received sentences to the console during startup (on until the
// app clears it after the config sequence completes)
volatile uint8_t gps_echo_enabled = 1;

// Steady-state passthrough of PQTMTXT/PQTMTAR/THS as # lines: debugging
// aid for a human watching the terminal, off by default. Toggled with
// the 'G' console key (or from the debugger). THS parsing for the CSV
// heading field is NOT gated by this — it always runs.
volatile uint8_t gps_passthrough_enabled = 0;

// Error recovery state
volatile uint32_t last_gga_timestamp = 0;
static uint32_t last_dma_position = 0;
static uint32_t last_dma_check_time = 0;

/**
 * @brief Store an identity-query reply payload (bounded, checksum stripped)
 * @param payload Sentence text just past the name (",LC29H...,date*XX")
 * @param dst     Destination string buffer
 * @param max     sizeof(dst)
 */
static void gps_capture_id(const char *payload, char *dst, uint32_t max)
{
  if (*payload == ',')
    payload++;
  uint32_t i = 0;
  while (payload[i] != '\0' && payload[i] != '*' && i < max - 1U)
  {
    dst[i] = payload[i];
    i++;
  }
  dst[i] = '\0';
}

// ============================================================================
// GPS Module Configuration
// ============================================================================

/**
 * @brief Send one NMEA sentence on USART2, appending the checksum
 * @param body Sentence content between '$' and '*' (no framing)
 *
 * Checksum is the XOR of every character in the body, per NMEA 0183.
 */
static void gps_send_nmea(const char *body)
{
  char msg[MINMEA_MAX_SENTENCE_LENGTH];
  uint8_t checksum = 0;

  for (const char *p = body; *p; p++)
    checksum ^= (uint8_t)*p;

  int len = snprintf(msg, sizeof(msg), "$%s*%02X\r\n", body, checksum);
  if (len > 0 && len < (int)sizeof(msg))
  {
    HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, HAL_MAX_DELAY);
    printf("# > %s", msg);  // msg already ends in \r\n; "# > " marks TX vs RX echo
  }
}

/**
 * @brief Configure the GPS module output (Quectel $PQTM commands)
 *
 * The module does not output data on this UART by default: enable NMEA
 * protocol, 50 ms (20 Hz) fix rate, GGA/GSV on and GSA/VTG/GLL/RMC off,
 * then save parameters and restart the module. Each command is acked
 * with an OK sentence, visible via the startup echo.
 */
void gps_send_config(void)
{
  static const char *cmds[] = {
	"PQTMCFGPROT,W,1,3,7,7",
	"PQTMCFGFIXRATE,W,50",
    "PQTMCFGMSGRATE,W,1,3,GGA,1",
    "PQTMCFGMSGRATE,W,1,3,GSV,1",
    "PQTMCFGMSGRATE,W,1,3,GSA,0",
    "PQTMCFGMSGRATE,W,1,3,VTG,0",
    "PQTMCFGMSGRATE,W,1,3,GLL,0",
    "PQTMCFGMSGRATE,W,1,3,RMC,0",
	"PQTMVERNO",     /* module FW version — reply captured for the 'I' info  */
	"PQTMUNIQID",    /* unique chip ID — ignored by modules that lack it     */
	"PQTMSAVEPAR",
	"PQTMSRR",
  };

  for (uint32_t i = 0; i < sizeof(cmds) / sizeof(cmds[0]); i++)
  {
    gps_send_nmea(cmds[i]);
    HAL_Delay(10);  // give the module time to process each command
  }
}

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
    // Queued, not printf'd: this runs in the USART2 IRQ, and a direct
    // printf could tear a CSV line the task is assembling (see the ring).
    gps_echo_enqueue("DMA HARDWARE STUCK - Last resort restart");
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

    // Startup diagnostics: echo everything the GPS says, #-prefixed so
    // the CSV parser skips it. Shows the OK acks for each config command.
    if (gps_echo_enabled)
      printf("# %s\r\n", sentence);
    // Dual-antenna / free-form text sentences: when passthrough is on
    // ('G' key), queued for the task to print as # lines (never printf'd
    // from this IRQ — see the ring's comment). THS is parsed below for
    // the CSV heading field regardless.
    else if (gps_passthrough_enabled &&
             (strncmp(sentence, "$PQTMTXT", 8) == 0 ||
              strncmp(sentence, "$PQTMTAR", 8) == 0 ||
              strncmp(sentence, "$GNTHS", 6) == 0 ||
              strncmp(sentence, "$GPTHS", 6) == 0))
    {
      gps_echo_enqueue(sentence);
    }

    // Parse GGA sentences (PRIORITY - checked first)
    if (strncmp(sentence, "$GPGGA", 6) == 0 || strncmp(sentence, "$GNGGA", 6) == 0)
    {
      gps_parse_gga_sentence(sentence);

      last_gga_timestamp = HAL_GetTick();
    }
    // Parse THS sentences (true heading + status, dual-antenna solution)
    else if (strncmp(sentence, "$GNTHS", 6) == 0 || strncmp(sentence, "$GPTHS", 6) == 0)
    {
      gps_parse_ths_sentence(sentence);
    }
    // Identity query replies (sent during gps_send_config). Checksum-
    // gated: these arrive exactly once and are stamped into every study
    // header, so a glitched reply must stay "?" rather than persist.
    else if (strncmp(sentence, "$PQTMVERNO", 10) == 0)
    {
      if (minmea_check(sentence, true))
        gps_capture_id(sentence + 10, gps_version_str, sizeof(gps_version_str));
    }
    else if (strncmp(sentence, "$PQTMUNIQID", 11) == 0)
    {
      if (minmea_check(sentence, true))
        gps_capture_id(sentence + 11, gps_uniqid_str, sizeof(gps_uniqid_str));
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
  uint32_t dma_write_pos = GPS_DMA_BUFFER_SIZE - __HAL_DMA_GET_COUNTER(huart2.hdmarx);
  if (dma_write_pos == gps_buffer_read_pos)
    return 0; // No new data

  uint32_t read_pos = gps_buffer_read_pos;
  uint32_t start_pos = read_pos;  // position of the current sentence's '$'
  uint32_t idx = 0;
  uint8_t found_start = 0;

  // Search for complete sentence: $....\n
  while (read_pos != dma_write_pos)
  {
    uint8_t c = gps_dma_buffer[read_pos];
    uint32_t c_pos = read_pos;
    read_pos = (read_pos + 1) % GPS_DMA_BUFFER_SIZE;

    if (c == '$')
    {
      // Any '$' (re)starts a sentence. A '$' mid-build means the previous
      // fragment was malformed: discard it but keep THIS sentence — the
      // old start-over-only-on-next-$ lost the sentence after a fragment.
      found_start = 1;
      idx = 0;
      output[idx++] = c;
      start_pos = c_pos;
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

  // No complete sentence. If one is PARTIALLY here (its tail still
  // streaming in — routine, since this runs while reception continues),
  // park the read pointer at its '$' so the next character-match rescans
  // the whole sentence once its LF has landed. The old code advanced
  // past the fragment, beheading every sentence whose first bytes
  // arrived during the previous sentence's parse: with RTK-length GGAs
  // at 20 Hz that killed the trailing $--THS on EVERY fix (heading
  // blank in the field), and without RTK intermittently. Junk before
  // any '$' is still consumed. Self-healing if the LF never comes: once
  // >= max_len bytes pile up, the overflow discard above resyncs at the
  // next '$'.
  gps_buffer_read_pos = found_start ? start_pos : read_pos;
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
 * @brief Parse THS sentence (true heading and status) into the heading cache
 * @param sentence NMEA THS sentence string, e.g. "$GNTHS,123.45,A*XX"
 *
 * Only status 'A' (autonomous — good solution on both antennas) is
 * accepted; 'V' (invalid), 'E' (estimated) and 'M' (manual) clear the
 * valid flag so the CSV heading field goes empty instead of reporting
 * a guess.
 */
void gps_parse_ths_sentence(const char *sentence)
{
  union minmea_type type;
  struct minmea_float heading;
  char status;

  // Checksum first: a UART glitch can corrupt a digit and still satisfy
  // the scanner's grammar, and a heading is worthless if it's wrong.
  // strict=true so a frame whose '*' itself got corrupted is rejected
  // rather than skipping verification (the module always sends *hh).
  if (minmea_check(sentence, true) &&
      minmea_scan(sentence, "tfc", &type, &heading, &status) &&
      status == 'A' && heading.scale != 0)
  {
    float deg = minmea_tofloat(&heading);
    if (deg >= 0.0f && deg <= 360.0f)
    {
      gps_heading_deg = deg;
      last_ths_timestamp = HAL_GetTick();
      gps_heading_valid = 1;
      return;
    }
  }
  gps_heading_valid = 0;
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
    /* Anchor tick: the newest COMPLETED frame — by pacing it fired
     * MD_SAMPLE_LEAD_MS before this fix and its ADC data is already in
     * the frame ring, so the anchor line always follows its sample line
     * on the wire. One atomic uint32 read; ADC IRQs outrank this one. */
    latest_gps_position.tick = md_tick_completed;

    // Check if position is valid (not NaN and has fix)
    if (!isnan(latest_gps_position.latitude) &&
        !isnan(latest_gps_position.longitude) &&
        frame.fix_quality > 0)
    {
      latest_gps_position.valid = 1;
      new_gga_available = 1;  // Signal new data available
      md_pace_on_fix();       // Re-sync detector sampling to this fix
    }
    else
    {
      latest_gps_position.valid = 0;
    }
  }
}
