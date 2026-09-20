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
// $PQTMUNIQID queries — sent during gps_send_config at boot and again by
// gps_query_identity when the 'I' key finds them still unanswered. Raw
// payload (after the sentence name, checksum stripped); empty string
// until the module answers. Written from the USART2 IRQ; the 'I' info
// report (task) snapshots both with that IRQ masked, because its deadline
// path prints while a late reply may still be landing (see md_print_info).
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
    // Bounded, not HAL_MAX_DELAY: since the 'I' re-query this also runs at
    // steady state, when the ISR's DMA-stuck watchdog may DeInit/Init
    // huart2 underneath a transmit in flight. ~30 bytes take 0.7 ms at
    // 460800; 20 ms is generous and a miss is reported, not hung on.
    HAL_StatusTypeDef st = HAL_UART_Transmit(&huart2, (uint8_t *)msg,
                                             (uint16_t)len, 20U);
    printf("# > %s", msg);  // msg already ends in \r\n; "# > " marks TX vs RX echo
    if (st != HAL_OK)
      printf("# > (gps tx failed, status %d)\r\n", (int)st);
  }
}

/**
 * The module's boot configuration (Quectel $PQTM commands)
 *
 * The module does not output data on this UART by default: enable NMEA
 * protocol, 50 ms (20 Hz) fix rate, GGA on and GSV/GSA/VTG/GLL/RMC off,
 * then save parameters and restart the module. Each command is acked
 * with an OK sentence, visible via the startup echo.
 *
 * GSV is OFF (2026-09-20): the firmware parsed and discarded it, and on
 * this module it arrives as a once-per-second block of 25-43 sentences
 * (1.6-2.8 kB, 34-61 ms of UART time at 460800) — the one thing in the
 * stream able to shift a GGA by tens of ms. With the pacer now free-
 * running that can no longer touch the coils, but it still costs ISR time
 * and displaces the anchor by v*delay once a second. Fields 3-4 of
 * PQTMCFGMSGRATE are PortType 1 (UART) and PortID 3 (this port).
 */
static const char *const gps_cfg_cmds[] = {
	"PQTMCFGPROT,W,1,3,7,7",
	"PQTMCFGFIXRATE,W,50",
    "PQTMCFGMSGRATE,W,1,3,GGA,1",
    "PQTMCFGMSGRATE,W,1,3,GSV,0",
    "PQTMCFGMSGRATE,W,1,3,GSA,0",
    "PQTMCFGMSGRATE,W,1,3,VTG,0",
    "PQTMCFGMSGRATE,W,1,3,GLL,0",
    "PQTMCFGMSGRATE,W,1,3,RMC,0",
	"PQTMVERNO",     /* module FW version — reply captured for the 'I' info  */
	"PQTMUNIQID",    /* unique chip ID — ignored by modules that lack it     */
	"PQTMSAVEPAR",
	"PQTMSRR",
};
#define GPS_CFG_CMD_COUNT  ((uint32_t)(sizeof gps_cfg_cmds / sizeof gps_cfg_cmds[0]))
#define GPS_CFG_CMD_GAP_MS 10U    /* the module needs a moment per command */

// ---- Did the burst reach the module? (2026-09-20) --------------------------
// The GPS shares the rig's supply but comes up slightly AFTER the MCU, so
// the boot burst (StartDefaultTask, a fixed 3 s after start) can go out to a
// module that is not listening yet — or that starts listening PART-way
// through the ~130 ms burst. It then runs whatever it PQTMSAVEPAR'd on some
// earlier boot (a partial burst even saves a stale-head/new-tail mix) —
// harmless until the list above changes (GSV off, a rate...), when the
// field unit silently keeps the old behaviour and the difference gets
// chased as an analog problem.
//
// Protocol facts (Quectel LG290P/LGx80P GNSS Protocol Specification): every
// Set command is answered with its own name — $PQTMCFGPROT,OK / ,ERROR,<n>,
// $PQTMCFGMSGRATE,OK ..., $PQTMSAVEPAR,OK; PQTMSRR has no reply; the module
// prints a $PQTMVER banner first thing on every startup, and its command
// parser is ready some unspecified time after that.
//
// So: the USART2 ISR counts $PQTMCFG* replies (OK or ERROR — either proves
// that command was heard) and notes the first checksum-valid sentence
// (module heard). A burst counts as DELIVERED only when every PQTMCFG
// command in the table was answered — one ack proves one command, not the
// burst. gps_config_poll (task loop) re-sends an undelivered burst once
// the module has been heard: GPS_CFG_SETTLE_MS after it was first heard,
// then, still not fully answered, again after growing gaps (parser not
// ready yet) — GPS_CFG_MAX_RESENDS attempts in all. Nothing ever waits for
// the module: a bench boot with the GPS off costs no extra time, and a
// module switched on minutes later still gets the current config (and a
// restart — expect a short GGA gap). Re-sends are stepped one command per
// poll (~0.7 ms of blocking TX each) rather than the boot burst's HAL_Delay
// loop, which at steady state would overrun the frame ring. Before PQTMSRR
// both paths give the PQTMSAVEPAR NVM write time to finish: its ack, or
// GPS_CFG_SAVE_WAIT_MS. A module that never says anything (off, unplugged,
// or factory-fresh with NMEA output disabled and up late) is never probed —
// reset the MCU with the module powered to provision one.
//
// ISR-written / task-read counters; the task never writes the ISR's. A
// byte flag and an aligned uint32 are each atomic on the M4, and the ISR
// runs to completion before the task resumes, so no masking is needed.
static volatile uint8_t  gps_heard       = 0;   /* ISR: a valid sentence arrived      */
static volatile uint32_t gps_heard_tick  = 0;   /* ISR: HAL tick of the first one     */
static volatile uint32_t gps_cfg_replies = 0;   /* ISR: $PQTMCFG* replies, running    */
static volatile uint32_t gps_cfg_saves   = 0;   /* ISR: $PQTMSAVEPAR replies, running */
static uint8_t  gps_cfg_attempts   = 0;         /* task: bursts sent — 1 boot, +1 per re-send */
static uint32_t gps_cfg_base       = 0;         /* task: gps_cfg_replies when the latest burst began */
static uint32_t gps_cfg_saves_base = 0;         /* task: gps_cfg_saves when its SAVEPAR went out */
static uint32_t gps_cfg_next       = GPS_CFG_CMD_COUNT; /* task: re-send cursor; == COUNT idle */
static uint32_t gps_cfg_due_tick   = 0;         /* task: next command due / reply window end */
static uint8_t  gps_cfg_reported   = 0;         /* task: outcome printed once */
#define GPS_CFG_MAX_RESENDS   3U
#define GPS_CFG_SETTLE_MS     500U   /* module first heard -> re-send 1           */
#define GPS_CFG_REPLY_WAIT_MS 1000U  /* last command of a burst -> it is judged   */
#define GPS_CFG_SAVE_WAIT_MS  200U   /* PQTMSAVEPAR -> PQTMSRR unless acked first */
/* extra wait before re-send k (1-based) once the previous reply window closed.
 * A stepped burst takes ~300 ms (10 x 10 ms + the 200 ms save gap) and is
 * judged 1 s after its last command, so with no acks at all the re-sends
 * start ~0.5 s, ~2.8 s and ~8.1 s after the module was first heard and the
 * verdict is in by ~9.4 s. */
static const uint32_t gps_cfg_retry_gap_ms[GPS_CFG_MAX_RESENDS] = { 0U, 1000U, 4000U };

static int gps_cfg_is_savepar(uint32_t i)
{
  return strcmp(gps_cfg_cmds[i], "PQTMSAVEPAR") == 0;
}

/* Number of PQTMCFG commands in the table = replies a delivered burst draws */
static uint32_t gps_cfg_needed(void)
{
  static uint32_t needed = 0U;
  if (needed == 0U)
    for (uint32_t i = 0; i < GPS_CFG_CMD_COUNT; i++)
      if (strncmp(gps_cfg_cmds[i], "PQTMCFG", 7) == 0)
        needed++;
  return needed;
}

/* Latest burst answered in full? */
static int gps_cfg_delivered(void)
{
  return gps_cfg_attempts > 0U &&
         (gps_cfg_replies - gps_cfg_base) >= gps_cfg_needed();
}

/**
 * @brief Send the configuration burst (boot; blocking, ~130 ms + save wait)
 */
void gps_send_config(void)
{
  gps_cfg_attempts++;
  gps_cfg_base = gps_cfg_replies;
  for (uint32_t i = 0; i < GPS_CFG_CMD_COUNT; i++)
  {
    uint32_t saves = gps_cfg_saves;
    gps_send_nmea(gps_cfg_cmds[i]);
    if (gps_cfg_is_savepar(i))
    {
      uint32_t t0 = HAL_GetTick();      /* NVM write: wait for the ack or the cap */
      while (gps_cfg_saves == saves && (HAL_GetTick() - t0) < GPS_CFG_SAVE_WAIT_MS)
        HAL_Delay(1);
    }
    HAL_Delay(GPS_CFG_CMD_GAP_MS);
  }
}

/**
 * @brief Re-send an undelivered burst once the module is heard (task loop, ~1 ms)
 */
void gps_config_poll(void)
{
  uint32_t now = HAL_GetTick();

  if (gps_cfg_next < GPS_CFG_CMD_COUNT)             /* a re-send is in flight */
  {
    /* The PQTMSAVEPAR -> PQTMSRR gap ends early once the save is acked */
    int save_gap = (gps_cfg_next > 0U) && gps_cfg_is_savepar(gps_cfg_next - 1U);
    if ((int32_t)(now - gps_cfg_due_tick) < 0 &&
        !(save_gap && gps_cfg_saves != gps_cfg_saves_base))
      return;
    if (gps_cfg_is_savepar(gps_cfg_next))
      gps_cfg_saves_base = gps_cfg_saves;
    gps_send_nmea(gps_cfg_cmds[gps_cfg_next]);
    uint32_t gap = gps_cfg_is_savepar(gps_cfg_next) ? GPS_CFG_SAVE_WAIT_MS
                                                    : GPS_CFG_CMD_GAP_MS;
    gps_cfg_next++;
    gps_cfg_due_tick = now + ((gps_cfg_next < GPS_CFG_CMD_COUNT)
                              ? gap : GPS_CFG_REPLY_WAIT_MS);
    return;
  }
  if (gps_cfg_attempts == 0U)                       /* boot burst not out yet */
    return;
  if (gps_cfg_delivered())                          /* configured — done */
  {
    if (gps_cfg_attempts >= 2U && !gps_cfg_reported)
    {
      gps_cfg_reported = 1U;
      printf("# GPS config re-send %u acknowledged in full (gps_cfg=resent)\r\n",
             (unsigned)(gps_cfg_attempts - 1U));
    }
    return;
  }
  if (!gps_heard)                                   /* off / unplugged / mute: nothing to wait for */
    return;
  if (gps_cfg_attempts > GPS_CFG_MAX_RESENDS)       /* every re-send spent */
  {
    if (!gps_cfg_reported && (int32_t)(now - gps_cfg_due_tick) >= 0)
    {
      gps_cfg_reported = 1U;
      printf("# GPS config: %lu of %lu commands acknowledged after %u re-sends "
             "(gps_cfg=noreply) - module may be running stale settings\r\n",
             (unsigned long)(gps_cfg_replies - gps_cfg_base),
             (unsigned long)gps_cfg_needed(), (unsigned)GPS_CFG_MAX_RESENDS);
    }
    return;
  }
  /* Next attempt: the first GPS_CFG_SETTLE_MS after the module was first
   * heard, later ones a growing gap after the previous reply window closed
   * (the module talks before its command parser is ready). */
  uint32_t k   = gps_cfg_attempts - 1U;             /* 0-based re-send index */
  uint32_t due = (k == 0U) ? gps_heard_tick + GPS_CFG_SETTLE_MS
                           : gps_cfg_due_tick + gps_cfg_retry_gap_ms[k];
  if ((int32_t)(now - due) < 0)
    return;
  printf("# GPS config re-send %lu/%u (%lu of %lu commands acknowledged so far; "
         "%s) - expect a short GGA gap while the module restarts\r\n",
         (unsigned long)(k + 1U), (unsigned)GPS_CFG_MAX_RESENDS,
         (unsigned long)(gps_cfg_replies - gps_cfg_base),
         (unsigned long)gps_cfg_needed(),
         (k == 0U) ? "module came up after the boot burst"
                   : "module not accepting commands yet");
  gps_cfg_attempts++;
  gps_cfg_base = gps_cfg_replies;
  gps_cfg_next = 0U;
  gps_cfg_due_tick = now;
}

/**
 * @brief Delivery state of the config burst, for the 'I' info line
 */
const char *gps_config_state(void)
{
  if (gps_cfg_delivered())
    return (gps_cfg_attempts >= 2U) ? "resent" : "boot";
  if (!gps_heard)
    return "silent";
  /* Latched by gps_config_poll the moment the last reply window closes
   * (not re-derived from the tick: a signed tick delta against a frozen
   * deadline turns negative again after 2^31 ms = 24.8 days of uptime).
   * The 'resent' branch also sets the flag, but only once delivered — and
   * delivered is monotonic, so that case returned above. */
  if (gps_cfg_attempts > GPS_CFG_MAX_RESENDS && gps_cfg_reported)
    return "noreply";
  return "pending";
}

/**
 * @brief Re-ask the module for its version and unique ID
 *
 * The GPS shares the rig's supply but comes up slightly AFTER the MCU, so
 * the boot-time queries above can go out before the module is listening:
 * GGA still flows (the module boots into its PQTMSAVEPAR'd config) but
 * gps_version_str / gps_uniqid_str stay empty and every study header
 * would carry gps_ver=? gps_id=? for the MCU's lifetime (2026-09-20).
 * The 'I' key calls this when either is still empty — by the time the
 * Pi's daemon connects and asks, the module has long been up. Two short
 * sentences, ~0.7 ms of blocking TX at 460800; the replies land in the
 * USART2 ISR like any other sentence (gps_process_dma_buffer).
 */
void gps_query_identity(void)
{
  gps_send_nmea("PQTMVERNO");
  gps_send_nmea("PQTMUNIQID");
}

/**
 * @brief 1 once both identity replies have been captured
 */
int gps_identity_known(void)
{
  return (gps_version_str[0] != '\0' && gps_uniqid_str[0] != '\0') ? 1 : 0;
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

    // Config-burst delivery tracking (see gps_config_poll): the first
    // checksum-valid sentence means the module is up and talking; each
    // $PQTMCFG* reply (OK or ERROR) is one config command heard; the
    // $PQTMSAVEPAR reply releases the wait before PQTMSRR. Checksum-gated
    // so line noise from a powered-down module never counts as "heard".
    // Replies to our own commands — and the module's $PQTMVER boot banner,
    // i.e. "I (re)started" — are queued as # lines once the startup echo
    // is off, so a steady-state re-send (or an 'I' identity re-query) is
    // as visible on the console as the boot burst.
    if (minmea_check(sentence, true))
    {
      if (!gps_heard)
      {
        gps_heard_tick = now;
        gps_heard = 1;
      }
      if (strncmp(sentence, "$PQTMCFG", 8) == 0)
        gps_cfg_replies++;
      else if (strncmp(sentence, "$PQTMSAVEPAR", 12) == 0)
        gps_cfg_saves++;
      if (!gps_echo_enabled &&
          (strncmp(sentence, "$PQTMCFG", 8) == 0 ||
           strncmp(sentence, "$PQTMSAVEPAR", 12) == 0 ||
           strncmp(sentence, "$PQTMVER", 8) == 0 ||     /* banner and $PQTMVERNO */
           strncmp(sentence, "$PQTMUNIQID", 11) == 0))
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
    // Parse GSV sentences (Satellites in View). GSV is disabled in
    // gps_send_config (2026-09-20); this branch stays only so a stray
    // sentence (config not yet applied) is consumed quietly — the
    // satellite count on the info line comes from GGA, not from here.
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
    /* Anchor tick: the newest COMPLETED frame. The pacer free-runs, so
     * that frame completed somewhere within the last sample interval
     * before this GGA parsed; its ADC data is already in the frame ring,
     * so the anchor line always follows its sample line on the wire, and
     * the host's tick-space interpolation tolerates the sub-interval
     * phase. One atomic uint32 read; ADC IRQs outrank this one. */
    latest_gps_position.tick = md_tick_completed;

    // Check if position is valid (not NaN and has fix)
    if (!isnan(latest_gps_position.latitude) &&
        !isnan(latest_gps_position.longitude) &&
        frame.fix_quality > 0)
    {
      latest_gps_position.valid = 1;
      new_gga_available = 1;  // Signal new data available
      md_pace_note_fix();     // Diagnostics only — a fix never moves the pacer
    }
    else
    {
      latest_gps_position.valid = 0;
    }
  }
}
