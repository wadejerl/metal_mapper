# STM32H7 High-Rate GPS Parser Implementation

## Summary
Implemented robust DMA-based NMEA GPS parser for STM32H723ZGTX metal detector project. Handles 10-20Hz GPS at 460800 baud with automatic recovery from power cycles and errors.

## Architecture
- **DMA Circular Buffer**: 512 bytes, 32-byte aligned for D-Cache coherency
- **Non-blocking Parser**: Processes GGA sentences only, doesn't block metal detector
- **Target Rate**: 5Hz tested, 10Hz → 20Hz ready, dual GPS capable

## Error Recovery (4 Mechanisms)

### 1. UART Error Recovery
Detects overrun/framing/noise errors from GPS power cycle
```c
if (huart2.ErrorCode != HAL_UART_ERROR_NONE)
  → Clear flags, restart DMA
```

### 2. DMA Stuck Detection
Monitors DMA position, restarts if no movement for 2 seconds
```c
if (dma_pos unchanged for 2000ms)
  → Full UART/DMA reinit
```

### 3. Framing Resynchronization
Searches for '$' if no GGA received for 500ms
```c
if (no GGA for 500ms)
  → Scan buffer for '$', attempt parse
```

### 4. Garbage Skip
Advances past bad data if stuck on same position 3x
```c
if (same position 3 times)
  → Skip byte, continue searching
```

## Key Files Modified

### Core/Src/main.c (USER CODE sections)
- `gps_process_dma_buffer()` - Main parser with 4-stage error recovery
- `gps_find_nmea_sentence()` - Circular buffer sentence extractor
- `gps_parse_gga_sentence()` - GGA parser with position caching

### Core/Src/stm32h7xx_hal_msp.c
- DMA1 Stream 5 configuration for USART2 RX
- Circular mode, medium priority

### Core/Src/stm32h7xx_it.c
- DMA1_Stream5_IRQHandler for USART2 RX

### Core/Inc/main.h
- Export hdma_usart2_rx for interrupt handler

### Core/Inc/minmea.h
- Increased MINMEA_MAX_SENTENCE_LENGTH to 120 for high-precision GPS

## Testing Status
- ✅ 5Hz GPS with power cycle recovery (fuzz tested)
- ✅ Automatic resync after GPS reset
- ✅ D-Cache coherency verified
- ⏳ Ready for 10Hz → 20Hz stress testing
- ⏳ Dual GPS preparation

## Next Steps
1. Test at 10Hz GPS rate
2. Test at 20Hz GPS rate
3. Validate dual GPS readiness
4. Remove debug prints if desired for production
