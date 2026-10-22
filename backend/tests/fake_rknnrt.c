/* Fake librknnrt.so for the RKNN tests on a PC (no NPU). Same C API and struct layout as rknn_api.h 2.3.2
 * (checked against the real header). Output: one person box at the input centre (cx=320 cy=320 w=100 h=200,
 * score 0.9) in a 1x84x8400 tensor; anchor 1: class 1 score = mean R / 255, class 2 score = mean B / 255.
 * Input uint8 or FP16 (0..255), output FP32 (want_float) or native FP16 - like the real runtime. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
typedef uint64_t ctx_t;
typedef struct { uint32_t n_input, n_output; } io_num;
typedef struct { uint32_t index, n_dims, dims[16]; char name[256]; uint32_t n_elems, size; int fmt, type, qnt_type;
                 int8_t fl; int32_t zp; float scale; uint32_t w_stride, size_with_stride; uint8_t pass_through;
                 uint32_t h_stride; } attr_t;
typedef struct { char api_version[256], drv_version[256]; } sdk_t;
typedef struct { uint32_t index; void *buf; uint32_t size; uint8_t pass_through; int type, fmt; } in_t;
typedef struct { uint8_t want_float, is_prealloc; uint32_t index; void *buf; uint32_t size; } out_t;
static const void *g_in; static uint32_t g_size; static int g_type;
int rknn_init(ctx_t *c, void *m, uint32_t n, uint32_t f, void *e) { if (!m || n < 16) return -6; *c = 0x1234; return 0; }
int rknn_destroy(ctx_t c) { return 0; }
int rknn_set_core_mask(ctx_t c, int m) { return (m == 0 || m == 1 || m == 2 || m == 4 || m == 7) ? 0 : -5; }
int rknn_query(ctx_t c, int cmd, void *info, uint32_t size) {
    if (cmd == 0) { ((io_num *)info)->n_input = 1; ((io_num *)info)->n_output = 1; return 0; }
    if (cmd == 1) { attr_t *a = info; a->n_dims = 4; a->dims[0] = 1; a->dims[1] = 640; a->dims[2] = 640; a->dims[3] = 3;
                    strcpy(a->name, "images"); a->n_elems = 640 * 640 * 3; a->size = a->n_elems; a->fmt = 1; a->type = 3; return 0; }
    if (cmd == 2) { attr_t *a = info; a->n_dims = 3; a->dims[0] = 1; a->dims[1] = 84; a->dims[2] = 8400;
                    strcpy(a->name, "output0"); a->n_elems = 84 * 8400; a->size = a->n_elems * 2; a->fmt = 0; a->type = 1; return 0; }
    if (cmd == 5) { strcpy(((sdk_t *)info)->api_version, "2.3.2 (fake)"); strcpy(((sdk_t *)info)->drv_version, "0.9.8"); return 0; }
    if (cmd == 4) { *(int64_t *)info = 1234; return 0; }
    return -5;
}
int rknn_inputs_set(ctx_t c, uint32_t n, in_t in[]) {
    if (n != 1 || in[0].fmt != 1) return -8;
    if (!((in[0].type == 3 && in[0].size == 640 * 640 * 3) || (in[0].type == 1 && in[0].size == 640 * 640 * 3 * 2))) return -8;
    g_in = in[0].buf; g_size = in[0].size; g_type = in[0].type; return 0;
}
int rknn_run(ctx_t c, void *e) { return g_in ? 0 : -1; }
int rknn_outputs_get(ctx_t c, uint32_t n, out_t out[], void *e) {
    uint32_t px = 640 * 640; double r = 0, b = 0;
    for (uint32_t i = 0; i < px * 3; i += 3) {
        if (g_type == 3) { r += ((const unsigned char *)g_in)[i]; b += ((const unsigned char *)g_in)[i + 2]; }
        else { r += (double)((const _Float16 *)g_in)[i]; b += (double)((const _Float16 *)g_in)[i + 2]; }
    }
    r /= px * 255.0; b /= px * 255.0;
    float *o = calloc(84 * 8400, 4);
    o[0] = 320; o[8400] = 320; o[2 * 8400] = 100; o[3 * 8400] = 200; o[4 * 8400] = 0.9f;
    o[5 * 8400 + 1] = (float)r; o[6 * 8400 + 1] = (float)b;
    if (out[0].want_float) { out[0].buf = o; out[0].size = 84 * 8400 * 4; return 0; }
    _Float16 *h = malloc(84 * 8400 * 2);
    for (int i = 0; i < 84 * 8400; i++) h[i] = (_Float16)o[i];
    free(o); out[0].buf = h; out[0].size = 84 * 8400 * 2; return 0;
}
int rknn_outputs_release(ctx_t c, uint32_t n, out_t out[]) { free(out[0].buf); return 0; }
