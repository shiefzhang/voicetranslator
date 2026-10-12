/*
 * vt_cpu_dispatch.c —— 静态多变体 CPU 后端的运行时调度（跨端共用）
 *
 * 这个文件是**唯一**以未改名形态提供 ggml_backend_cpu_reg() 的地方。
 * ggml-base 的注册表（ggml-backend-reg.cpp:165 的 `register_backend(ggml_backend_cpu_reg())`）
 * 只会看到它，由它按当前 CPU 的**实际**特性位挑一个静态变体。
 *
 * 为什么需要它
 * ------------
 * iPad mini 4（A8，无 dotprod）与 iPhone 16（A18，dotprod+i8mm）对指令集的要求是互斥的：
 * 以 dotprod 编的二进制在 A8 上会 SIGILL，以 armv8-a 编的又在 A18 上白扔 2~5 倍算力。
 * 上游的多变体方案（GGML_CPU_ALL_VARIANTS）强制依赖 GGML_BACKEND_DL → BUILD_SHARED_LIBS，
 * 会把整个 llama/ggml 变成一堆 dylib，动到 iOS 的打包与签名链路；本文件配套的
 * `scripts/vt-cpu-variants.py` 走的是静态路线：N 份 libggml-cpu-<tag>.a，非选中变体的
 * 全局符号统一加 `vtv<i>_` 前缀，于是链接期不再冲突，运行时只走一份。
 *
 * 关键性质
 * --------
 * 1) **未选中的变体只是躺在二进制里，一行都不会执行** —— A8 上永远碰不到 dotprod 指令。
 *    这是「兼容 mini4」的充要条件（不是「应该没问题」，是控制流上到不了）。
 * 2) **失败必须能回落**：特性探测拿不到值、表为空、入口返回空 —— 一律退回第一个变体
 *    （架构列表的第一项恒为 armv8-a 基线），绝不返回 NULL 让上层崩。
 * 3) 允许 `VT_CPU_VARIANT=<tag>` 强制指定，只为验证与排查；非法值不报错，回落自动档。
 *    验 A8 路径时靠它：在 A18 的模拟器上强制跑 armv8a 变体 —— 代码就是 A8 要跑的那份。
 */

#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#include "ggml-backend.h"
#include "vt_cpu_variants.h"   /* 由 scripts/vt-cpu-variants.py 生成 */

/* ------------------------------------------------------------------ */
/* 各变体入口（符号名已被 -include vt_ns_<tag>.h 加过前缀）            */
/* ------------------------------------------------------------------ */

#define VT_DECL_REG(i) extern ggml_backend_reg_t VT_CPU_REG(i)(void);
VT_DECL_REG(0)
#if VT_CPU_VARIANT_COUNT > 1
VT_DECL_REG(1)
#endif
#if VT_CPU_VARIANT_COUNT > 2
VT_DECL_REG(2)
#endif
#if VT_CPU_VARIANT_COUNT > 3
VT_DECL_REG(3)
#endif

typedef ggml_backend_reg_t (*vt_cpu_reg_fn)(void);

static vt_cpu_reg_fn const k_reg_fns[VT_CPU_VARIANT_COUNT] = VT_CPU_VARIANT_REG_LIST;

/* ------------------------------------------------------------------ */
/* CPU 特性探测                                                        */
/* ------------------------------------------------------------------ */

#if defined(__aarch64__)
#  if defined(__APPLE__)
#    include <sys/sysctl.h>
#  elif defined(__linux__)
#    include <sys/auxv.h>
#    ifndef HWCAP_ASIMDDP
#      define HWCAP_ASIMDDP (1 << 20)
#    endif
#    ifndef HWCAP2_I8MM
#      define HWCAP2_I8MM (1 << 13)
#    endif
#    ifndef HWCAP_FPHP
#      define HWCAP_FPHP (1 << 9)
#    endif
#  endif
#endif

static unsigned vt_cpu_features(void) {
    unsigned f = 0;

#if defined(__aarch64__) && defined(__APPLE__)
    /* Apple 只认这两条 sysctl；且**不能按架构版本推断**：
       A11/A12 标称 ARMv8.2-A 却没有任何 dotprod（clang 的 apple-a11/apple-a12
       特性表里就没有 FeatureDotProd），漏判这一档的后果是 SIGILL。 */
    int v = 0;
    size_t sz = sizeof(v);
    if (sysctlbyname("hw.optional.arm.FEAT_DotProd", &v, &sz, NULL, 0) == 0 && v) {
        f |= VT_FEAT_DOTPROD;
    }
    v = 0; sz = sizeof(v);
    if (sysctlbyname("hw.optional.arm.FEAT_I8MM", &v, &sz, NULL, 0) == 0 && v) {
        f |= VT_FEAT_I8MM;
    }
    v = 0; sz = sizeof(v);
    if (sysctlbyname("hw.optional.arm.FEAT_SME", &v, &sz, NULL, 0) == 0 && v) {
        f |= VT_FEAT_SME;
    }
#elif defined(__aarch64__) && defined(__linux__)
    unsigned long hwcap = getauxval(AT_HWCAP);
    unsigned long hwcap2 = getauxval(AT_HWCAP2);
    if (hwcap & HWCAP_ASIMDDP) { f |= VT_FEAT_DOTPROD; }
    if (hwcap & HWCAP_FPHP)    { f |= VT_FEAT_FP16_VA; }
    if (hwcap2 & HWCAP2_I8MM)  { f |= VT_FEAT_I8MM; }
#endif

    return f;
}

/* ------------------------------------------------------------------ */
/* 选择                                                                */
/* ------------------------------------------------------------------ */

static int  g_selected = -1;      /* 选中的变体下标；-1 = 还没算 */
static char g_why[128] = "";      /* 选择理由，供日志/探针打印 */

/* 解析 VT_CPU_VARIANT；返回下标，无法识别返回 -1。
   非法值**不报错**：宁可回落自动档，也不要因为一个环境变量把 App 变成砖。 */
static int vt_cpu_forced(const char * want) {
    if (want == NULL || want[0] == '\0' || strcmp(want, "auto") == 0) {
        return -1;
    }
    for (int i = 0; i < VT_CPU_VARIANT_COUNT; ++i) {
        if (strcmp(k_vt_cpu_variants[i].tag, want) == 0) {
            return i;
        }
    }
    return -1;
}

/* 变体优先序（=`k_vt_cpu_variants[i].score`，由生成头给出）+ 一处平台修正。
 *
 * 🔴 实测（2026-09-30，A18 Pro / 生产 Q4_K_M 包 / n_threads=2 / 3 次重复取中位，
 *    见 ENGINEERING-NOTES A.8）：**i8mm 档比 dotprod 档慢** ——
 *      冷 prefill 72.2 vs 75.5 t/s（−4.4%），生成 27.3 vs 29.5 t/s（−7.5%）。
 *    Q4_0 上也是同一方向（35.7 vs 37.5 t/s）。三次重复一致，不是噪声。
 *    原因可见：`__ARM_FEATURE_MATMUL_INT8` 会把 repack 的 kernel **整段换掉**
 *    （arch/arm/repack.cpp:2327-2724 与 4083-4515 那两块 smmla 实现），
 *    而这套 smmla 实现在 Apple 的核上反而不如 SDOT 版。
 *
 * 所以按 score 直接取最大会在 Apple 上选错。这里对「多要 I8MM 这一位」的变体降权，
 * 让它与基线同分 —— 有 dotprod 时必定选 dotprod，只有 dotprod 也没有时才回落 armv8a。
 * `VT_CPU_VARIANT=i8mm` 强制档不受影响，仍可用来复核这个结论。
 *
 * 非 Apple（Cortex-A7xx / X 系）**没有实测数据**，仍按上游口径走原始 score。 */
static int vt_cpu_score(int i) {
    int s = k_vt_cpu_variants[i].score;
#if defined(__APPLE__)
    if ((k_vt_cpu_variants[i].requires & VT_FEAT_I8MM) != 0) {
        s = 1;
    }
#endif
    return s;
}

static int vt_cpu_pick(void) {
    if (g_selected >= 0) {
        return g_selected;
    }

    const int forced = vt_cpu_forced(getenv("VT_CPU_VARIANT"));
    const unsigned feats = vt_cpu_features();

    if (forced >= 0) {
        g_selected = forced;
        snprintf(g_why, sizeof(g_why), "被 VT_CPU_VARIANT=%s 强制",
                 k_vt_cpu_variants[forced].tag);
        fprintf(stderr, "[vt-cpu] 变体 %s（%s；本机特性掩码 0x%x）\n",
                k_vt_cpu_variants[g_selected].tag, g_why, feats);
        return g_selected;
    }

    int best = 0;              /* 下标 0 恒为基线，兜底一定可用 */
    for (int i = 0; i < VT_CPU_VARIANT_COUNT; ++i) {
        const unsigned need = k_vt_cpu_variants[i].requires;
        if ((feats & need) != need) {
            continue;          /* 缺特性：这个变体在这台机器上不许碰 */
        }
        if (vt_cpu_score(i) > vt_cpu_score(best)) {
            best = i;
        }
    }

    g_selected = best;
#if defined(__APPLE__)
    snprintf(g_why, sizeof(g_why), "按特性掩码 0x%x 自动选择（Apple 下 i8mm 档已按实测降权）", feats);
#else
    snprintf(g_why, sizeof(g_why), "按特性掩码 0x%x 自动选择", feats);
#endif
    fprintf(stderr, "[vt-cpu] 变体 %s（%s）\n",
            k_vt_cpu_variants[g_selected].tag, g_why);
    return g_selected;
}

/* ggml-base 唯一需要的入口。 */
ggml_backend_reg_t ggml_backend_cpu_reg(void) {
    const int i = vt_cpu_pick();
    ggml_backend_reg_t reg = k_reg_fns[i]();
    if (reg == NULL) {
        /* 选中的变体起不来（理论上不会），退回基线，别让上层拿到 NULL。 */
        if (i != 0) {
            g_selected = 0;
            snprintf(g_why, sizeof(g_why), "变体 %s 起不来，回落基线",
                     k_vt_cpu_variants[i].tag);
            reg = k_reg_fns[0]();
        }
    }
    return reg;
}

/* ---------------------- 供日志 / 探针使用 ---------------------- */

const char * vt_cpu_selected_tag(void) {
    return k_vt_cpu_variants[vt_cpu_pick()].tag;
}

const char * vt_cpu_selected_reason(void) {
    vt_cpu_pick();
    return g_why;
}

unsigned long vt_cpu_feature_mask(void) {
    return (unsigned long) vt_cpu_features();
}

int vt_cpu_variant_total(void) {
    return VT_CPU_VARIANT_COUNT;
}

const char * vt_cpu_variant_tag(int i) {
    if (i < 0 || i >= VT_CPU_VARIANT_COUNT) {
        return NULL;
    }
    return k_vt_cpu_variants[i].tag;
}

unsigned long vt_cpu_variant_requires(int i) {
    if (i < 0 || i >= VT_CPU_VARIANT_COUNT) {
        return 0;
    }
    return (unsigned long) k_vt_cpu_variants[i].requires;
}
