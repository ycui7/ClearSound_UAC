/* Compat shim: lib_audio_dsp's test tree includes the pre-relocation path
 * "xmath/xs3/vpu_scalar_ops.h"; the installed lib_xcore_math ships it under
 * "xmath/vpu/". Forward rather than patch vendor sources.
 * Drop this dir on the include path with -I. */
#ifndef CSD_XS3_COMPAT_VPU_SCALAR_OPS_H
#define CSD_XS3_COMPAT_VPU_SCALAR_OPS_H
#include "xmath/vpu/vpu_scalar_ops.h"
#endif
