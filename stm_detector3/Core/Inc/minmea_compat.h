/*
 * minmea_compat.h — platform shim for minmea on bare-metal newlib.
 *
 * Newlib ships no timegm() (no prototype, no symbol in libc). With no
 * timezone configured on bare metal, mktime() performs a straight UTC
 * conversion (minmea zeroes tm_isdst), so it is an exact substitute.
 * Pulled in via minmea.h's MINMEA_INCLUDE_COMPAT hook.
 */
#ifndef MINMEA_COMPAT_H
#define MINMEA_COMPAT_H

#include <time.h>

#define timegm mktime

#endif /* MINMEA_COMPAT_H */
