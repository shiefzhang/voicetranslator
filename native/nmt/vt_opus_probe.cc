// vt_opus_probe.cc —— macOS 上的端到端验收工具（不进 App）。
//
// 作用：把 native/nmt 这一份共享 C++ 核心直接跑在 macOS 的 ONNX Runtime 上，
// 用与 Python 门禁脚本同一批句子做逐字对比。只有这里对上了，才谈得上移植成功。
//
// 用法：vt_opus_probe <包目录> <TSV: 源语 目标语 句子>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "vt_opus.h"

int main(int argc, char **argv) {
  if (argc != 3) {
    std::cerr << "usage: vt_opus_probe <package-dir> <cases.tsv>\n";
    return 2;
  }
  char error[512] = {0};
  VtOpus *opus = vt_opus_load(argv[1], 4, error, sizeof(error));
  if (!opus) {
    std::cerr << "加载失败：" << error << "\n";
    return 1;
  }
  char *description = vt_opus_describe(opus);
  std::cerr << "已装载：" << (description ? description : "(null)") << "\n";
  vt_opus_free_string(description);

  std::ifstream input(argv[2]);
  std::string line;
  while (std::getline(input, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line.empty()) continue;
    std::istringstream parts(line);
    std::string source_lang, target_lang, text;
    if (!std::getline(parts, source_lang, '\t')) continue;
    if (!std::getline(parts, target_lang, '\t')) continue;
    if (!std::getline(parts, text)) continue;

    char *translated = vt_opus_translate(opus, text.c_str(), source_lang.c_str(),
                                         target_lang.c_str(), error, sizeof(error));
    std::cout << source_lang << "\t" << target_lang << "\t" << text << "\t"
              << (translated ? translated : ("<失败> " + std::string(error))) << "\n";
    std::cout.flush();
    if (translated) vt_opus_free_string(translated);
  }
  vt_opus_close(opus);
  return 0;
}
