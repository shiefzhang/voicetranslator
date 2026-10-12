cmake_minimum_required(VERSION 3.22)
project(vt LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_C_FLAGS_DEBUG "-O3 -g" CACHE STRING "" FORCE)
set(CMAKE_CXX_FLAGS_DEBUG "-O3 -g" CACHE STRING "" FORCE)
# Android's toolchain also supplies normal-scope flags; cache flags alone can
# be shadowed. Apply optimization to this directory and all inference targets.
add_compile_options(-O3)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(LLAMA_CURL OFF CACHE BOOL "" FORCE)
set(GGML_OPENMP OFF CACHE BOOL "" FORCE)
set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
set(GGML_CPU_ARM_ARCH armv8-a CACHE STRING "" FORCE)
add_subdirectory(../../../../third_party/llama.cpp llama)
add_library(vt-llama SHARED translator.cpp)
target_link_libraries(vt-llama PRIVATE llama android log)
target_link_options(vt-llama PRIVATE "-Wl,-z,max-page-size=16384" "-Wl,-z,common-page-size=16384")

# ---------------------------------------------------------------------------
# 专用翻译引擎（OPUS-MT / Marian），libvt-opus.so
#
# 与 iOS 侧编的是**同一份**源文件（native/nmt/），所以两端译文逐字一致 ——
# 这份一致性由 scripts/verify-opus-package.py 做回归（20/20 逐字相同）。
#
# ONNX Runtime **不在这里链接**：它在 sherpa-onnx 的 AAR 里
# （jni/arm64-v8a/libonnxruntime.so，21.7 MB，已随 APK 分发），
# 由 native/nmt/vt_ort.h 走 dlopen 拿 OrtGetApiBase（VT_ORT_DYNAMIC）。
# 这样就不必「从 AAR 里解出 .so 喂给 CMake、同时保证不重复打包」——
# 那套 Gradle 编排的收益是零，风险却不小。
# ---------------------------------------------------------------------------
add_library(vt-opus SHARED
        vt_opus_jni.cpp
        ../../../../native/nmt/vt_opus.cc
        ../../../../native/nmt/third_party/simple-sentencepiece/ssentencepiece/csrc/ssentencepiece.cc)
target_include_directories(vt-opus PRIVATE
        ../../../../native/nmt
        ../../../../native/nmt/third_party/simple-sentencepiece
        ../../../../native/nmt/third_party/onnxruntime)
target_compile_definitions(vt-opus PRIVATE VT_ORT_DYNAMIC)
target_link_libraries(vt-opus PRIVATE android log)
target_link_options(vt-opus PRIVATE "-Wl,-z,max-page-size=16384" "-Wl,-z,common-page-size=16384")
