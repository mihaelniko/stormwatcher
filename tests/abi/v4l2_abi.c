/* Prints the V4L2 ABI as seen by the C compiler: struct sizes, the offsets
 * the Python ctypes mirror relies on, and ioctl request numbers.
 * tests/test_v4l2_abi.py compiles this and compares it with stormwatch.v4l2. */
#include <stdio.h>
#include <stddef.h>
#include <linux/videodev2.h>

#define S(t) printf("size %s %zu\n", #t, sizeof(struct t))
#define O(t, f) printf("off %s.%s %zu\n", #t, #f, offsetof(struct t, f))
#define I(n) printf("ioctl %s %lu\n", #n, (unsigned long)(n))
#define C(n) printf("const %s %lu\n", #n, (unsigned long)(n))

int main(void) {
    S(v4l2_capability); O(v4l2_capability, capabilities); O(v4l2_capability, device_caps);
    S(v4l2_fmtdesc); O(v4l2_fmtdesc, pixelformat);
    S(v4l2_frmsizeenum); O(v4l2_frmsizeenum, discrete);
    S(v4l2_frmivalenum); O(v4l2_frmivalenum, discrete);
    S(v4l2_pix_format); O(v4l2_pix_format, bytesperline); O(v4l2_pix_format, sizeimage);
    S(v4l2_format); O(v4l2_format, fmt);
    S(v4l2_requestbuffers);
    S(v4l2_buffer); O(v4l2_buffer, timestamp); O(v4l2_buffer, sequence); O(v4l2_buffer, m);
    O(v4l2_buffer, length);
    S(v4l2_streamparm); O(v4l2_streamparm, parm);
    S(v4l2_captureparm); O(v4l2_captureparm, timeperframe);
    S(v4l2_control); S(v4l2_queryctrl); O(v4l2_queryctrl, minimum); O(v4l2_queryctrl, flags);
    I(VIDIOC_QUERYCAP); I(VIDIOC_ENUM_FMT); I(VIDIOC_G_FMT); I(VIDIOC_S_FMT);
    I(VIDIOC_REQBUFS); I(VIDIOC_QUERYBUF); I(VIDIOC_QBUF); I(VIDIOC_DQBUF);
    I(VIDIOC_STREAMON); I(VIDIOC_STREAMOFF); I(VIDIOC_G_PARM); I(VIDIOC_S_PARM);
    I(VIDIOC_G_CTRL); I(VIDIOC_S_CTRL); I(VIDIOC_QUERYCTRL);
    I(VIDIOC_ENUM_FRAMESIZES); I(VIDIOC_ENUM_FRAMEINTERVALS);
    C(V4L2_CID_EXPOSURE_AUTO); C(V4L2_CID_EXPOSURE_ABSOLUTE); C(V4L2_CID_EXPOSURE_AUTO_PRIORITY);
    C(V4L2_CID_AUTOGAIN); C(V4L2_CID_GAIN); C(V4L2_EXPOSURE_MANUAL);
    C(V4L2_CAP_VIDEO_CAPTURE); C(V4L2_CAP_STREAMING); C(V4L2_CAP_DEVICE_CAPS);
    C(V4L2_BUF_FLAG_TIMESTAMP_MASK); C(V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC); C(V4L2_BUF_FLAG_ERROR);
    C(V4L2_CAP_TIMEPERFRAME); C(V4L2_CTRL_FLAG_DISABLED);
    return 0;
}
