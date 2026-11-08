/* Forwarding header. The upsampling core lives in src/dsp/ (alongside its
 * generated tables), which XMOS cmake globs for SOURCES but does not add to the
 * include path; src/extensions/ is on the path. A quoted relative include
 * resolves against this file's directory, so the real header and its
 * "csd_dsp_taps.h" both still resolve normally. */
#ifndef CSD_UPSAMPLE_FWD_H
#define CSD_UPSAMPLE_FWD_H
#include "../dsp/csd_upsample.h"
#endif /* CSD_UPSAMPLE_FWD_H */
