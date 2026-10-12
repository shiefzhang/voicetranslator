// vt_opus.cc —— 端侧 OPUS-MT(Marian) 推理核心，两端共用。
//
// 移植自 scripts/check-nmt-quality.py 的 `MarianEngine`（Python 参考实现已在
// 2026-09-28 的门禁里对过 20 条 / 4 语向）。以下三条 merged-decoder 铁律一个都不能省：
//
//   ① 每个 past 槽必须是**独立**张量（共用会让 ORT 的 If 分支崩）；
//   ② encoder 侧 past 给**真实源句长**、decoder 侧给 0；
//   ③ **encoder 槽只在第 0 步更新一次** —— 增量分支返回的 `present.*.encoder.*`
//      是空张量，每步都覆盖会把真实 cross-attention KV 换成垃圾
//      （症状：前 2 个 token 正常，第 3 个起崩）。
//
// 另外两条来自实测的硬约束：
//   • `eos` / `decoder_start` / `pad` / `bad_words` **一律从 route.json 读**，不许写死。
//     base 族是 eos=0/start=65000，tc-big 是 eos=2/start=32000；
//     任何一个硬编码都会静默译错。
//   • 端侧 sentencepiece（ssentencepiece）**不做 NMT-NFKC 规范化**，而原始文本里
//     的中文全角标点会被整片切成 `<unk>`。所以在分词前必须套一层 nfkc.bin。
//     实测（21 条边界用例：全角标点 / 弯引号 / 罗马数字 / 半角片假名）：
//     `unicodedata.normalize("NFKC", s)` 与 sentencepiece 的输出**逐 id 相同**。

#include "vt_opus.h"

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "ssentencepiece/csrc/ssentencepiece.h"
#include "vt_ort.h"

namespace {

constexpr int kDefaultThreads = 2;   // 与 llama.cpp 侧一致（A8 双核已饱和）
constexpr int kMaxNewTokens = 128;   // 与门禁脚本一致
// U+2581 ▁ 的 UTF-8 编码，sentencepiece 的词间标记
constexpr char kSpaceMark[] = "\xe2\x96\x81";

void SetErr(std::string *err, const char *fmt, ...) {
  if (!err) return;
  char buffer[512];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buffer, sizeof(buffer), fmt, args);
  va_end(args);
  *err = buffer;
}

std::string Join(const std::string &dir, const std::string &name) {
  if (dir.empty()) return name;
  if (dir.back() == '/') return dir + name;
  return dir + "/" + name;
}

bool ReadFile(const std::string &path, std::string *out) {
  FILE *file = fopen(path.c_str(), "rb");
  if (!file) return false;
  if (fseek(file, 0, SEEK_END) != 0) { fclose(file); return false; }
  long size = ftell(file);
  if (size < 0) { fclose(file); return false; }
  rewind(file);
  out->resize(static_cast<size_t>(size));
  size_t read = size ? fread(&(*out)[0], 1, static_cast<size_t>(size), file) : 0;
  fclose(file);
  out->resize(read);
  return read == static_cast<size_t>(size);
}

bool IsFile(const std::string &path) {
  FILE *file = fopen(path.c_str(), "rb");
  if (!file) return false;
  fclose(file);
  return true;
}

// ---------------------------------------------------------------- 极简 JSON 取值
// route.json 是本项目自己产出的固定形状，为它引一个 JSON 库不划算。

bool JsonFind(const std::string &text, const char *key, size_t *pos) {
  const std::string needle = std::string("\"") + key + "\"";
  size_t at = text.find(needle);
  if (at == std::string::npos) return false;
  at = text.find(':', at + needle.size());
  if (at == std::string::npos) return false;
  *pos = at + 1;
  return true;
}

bool JsonInt(const std::string &text, const char *key, long long *out) {
  size_t pos = 0;
  if (!JsonFind(text, key, &pos)) return false;
  char *end = nullptr;
  long long value = strtoll(text.c_str() + pos, &end, 10);
  if (end == text.c_str() + pos) return false;
  *out = value;
  return true;
}

bool JsonString(const std::string &text, const char *key, std::string *out) {
  size_t pos = 0;
  if (!JsonFind(text, key, &pos)) return false;
  size_t open = text.find('"', pos);
  if (open == std::string::npos) return false;
  size_t close = text.find('"', open + 1);
  if (close == std::string::npos) return false;
  *out = text.substr(open + 1, close - open - 1);
  return true;
}

bool JsonIntArray(const std::string &text, const char *key, std::vector<int> *out) {
  size_t pos = 0;
  if (!JsonFind(text, key, &pos)) return false;
  size_t open = text.find('[', pos);
  if (open == std::string::npos) return false;
  size_t close = text.find(']', open);
  if (close == std::string::npos) return false;
  size_t index = open + 1;
  while (index < close) {
    char *end = nullptr;
    long value = strtol(text.c_str() + index, &end, 10);
    if (end == text.c_str() + index) { ++index; continue; }
    out->push_back(static_cast<int>(value));
    index = static_cast<size_t>(end - text.c_str());
  }
  return true;
}

// ---------------------------------------------------------------- NFKC 规范化

/// NMT-NFKC 的逐码点差分表。见文件头说明 —— 少了它中文全角标点全变 `<unk>`。
class Nfkc {
 public:
  bool Load(const std::string &path, std::string *err) {
    std::string blob;
    if (!ReadFile(path, &blob)) {
      SetErr(err, "读不到 NFKC 表：%s", path.c_str());
      return false;
    }
    if (blob.size() < 12 || memcmp(blob.data(), "VTNF", 4) != 0) {
      SetErr(err, "NFKC 表 magic 不对：%s", path.c_str());
      return false;
    }
    uint32_t version = 0, count = 0;
    memcpy(&version, blob.data() + 4, 4);
    memcpy(&count, blob.data() + 8, 4);
    if (version != 1) {
      SetErr(err, "NFKC 表版本不支持：%u", version);
      return false;
    }
    size_t cursor = 12;
    for (uint32_t i = 0; i < count; ++i) {
      if (cursor + 2 > blob.size()) { SetErr(err, "NFKC 表截断"); return false; }
      const uint8_t src_len = static_cast<uint8_t>(blob[cursor]);
      const uint8_t dst_len = static_cast<uint8_t>(blob[cursor + 1]);
      cursor += 2;
      if (cursor + src_len + dst_len > blob.size()) { SetErr(err, "NFKC 表截断"); return false; }
      std::string src = blob.substr(cursor, src_len);
      cursor += src_len;
      std::string dst = blob.substr(cursor, dst_len);
      cursor += dst_len;
      if (!src.empty()) {
        // 表已按「长度降序、同长按字节序」排好，分桶时保持原顺序即维护了最长优先
        buckets_[static_cast<uint8_t>(src[0])].push_back({std::move(src), std::move(dst)});
      }
    }
    loaded_ = true;
    return true;
  }

  /// 未命中就按码点原样复制。表里的 src 首字节都是 UTF-8 前导字节（0xC2..0xF4），
  /// 与后续字节区间（0x80..0xBF）不重叠，所以逐字节推进也不会误匹配。
  std::string Apply(const std::string &text) const {
    if (!loaded_) return text;
    std::string out;
    out.reserve(text.size() + 8);
    size_t index = 0;
    while (index < text.size()) {
      const auto &bucket = buckets_[static_cast<uint8_t>(text[index])];
      // 命中就只写替换结果；没命中才原样搬运一个码点。
      // ⚠️ 这两件事必须互斥 —— 曾经写成「先 append 替换串、再无条件 append 原文」，
      //    症状是全角标点被替换后原文又留下三字节，端侧切成 3 个 <unk>，
      //    译文退化成通顺但完全无关的句子（找了很久）。
      bool matched = false;
      for (const auto &entry : bucket) {
        if (entry.src.size() <= text.size() - index &&
            memcmp(text.data() + index, entry.src.data(), entry.src.size()) == 0) {
          out += entry.dst;
          index += entry.src.size();
          matched = true;
          break;
        }
      }
      if (!matched) {
        size_t length = Utf8Length(text[index]);
        if (length == 0 || index + length > text.size()) length = 1;
        out.append(text, index, length);
        index += length;
      }
    }
    return out;
  }

 private:
  struct Entry {
    std::string src;
    std::string dst;
  };

  static size_t Utf8Length(unsigned char lead) {
    if (lead < 0x80) return 1;
    if ((lead & 0xE0) == 0xC0) return 2;
    if ((lead & 0xF0) == 0xE0) return 3;
    if ((lead & 0xF8) == 0xF0) return 4;
    return 1;
  }

  std::vector<Entry> buckets_[256];
  bool loaded_ = false;
};

// ---------------------------------------------------------------- 单个语向

/// OrtValue 的作用域守卫。ORT 的 C API 没有 RAII，失败路径上漏一个张量
/// 就是每次失败几百 MB 的泄漏 —— 这里统一收口。
struct ValueRelease {
  const OrtApi *api;
  OrtValue *value = nullptr;
  ValueRelease(const OrtApi *a, OrtValue *v) : api(a), value(v) {}
  ~ValueRelease() {
    if (value != nullptr) api->ReleaseValue(value);
  }
  ValueRelease(const ValueRelease &) = delete;
  ValueRelease &operator=(const ValueRelease &) = delete;
};

/// 一个 OPUS-MT 单包（一个语向）的 ONNX 会话 + 分词 + id 映射。
class Route {
 public:
  ~Route() {
    const OrtApi *api = vt::Api();
    if (!api) return;
    if (dec_) api->ReleaseSession(dec_);
    if (enc_) api->ReleaseSession(enc_);
  }

  bool Load(const OrtApi *api, OrtEnv *env, const std::string &dir, int threads,
            std::string *err) {
    api_ = api;
    if (api->GetAllocatorWithDefaultOptions(&alloc_) != nullptr) {
      SetErr(err, "拿不到 ORT 默认分配器");
      return false;
    }

    std::string cfg_text;
    if (!ReadFile(Join(dir, "route.json"), &cfg_text)) {
      SetErr(err, "缺 route.json：%s", dir.c_str());
      return false;
    }
    long long value = 0;
    if (!JsonInt(cfg_text, "eosId", &value)) {
      SetErr(err, "route.json 缺 eosId");
      return false;
    }
    eos_id_ = static_cast<int>(value);
    JsonInt(cfg_text, "decoderStartId", &value);
    start_id_ = static_cast<int>(value);
    JsonInt(cfg_text, "padId", &value);
    pad_id_ = static_cast<int>(value);
    JsonInt(cfg_text, "decoderLayers", &value);
    layers_ = static_cast<int>(value);
    JsonInt(cfg_text, "decoderHeads", &value);
    heads_ = static_cast<int>(value);
    JsonInt(cfg_text, "headDim", &value);
    head_dim_ = static_cast<int>(value);
    JsonInt(cfg_text, "modelVocabSize", &value);
    model_vocab_ = static_cast<int>(value);
    JsonIntArray(cfg_text, "badWordIds", &bad_ids_);
    JsonString(cfg_text, "idMode", &id_mode_);
    if (heads_ <= 0 || head_dim_ <= 0 || layers_ <= 0 || model_vocab_ <= 0) {
      SetErr(err, "route.json 里的形状参数不合法");
      return false;
    }

    // —— 分词：ssentencepiece 只负责「源 spm 原生 id」，两张映射表负责跨 id 空间
    if (!IsFile(Join(dir, "source.vocab.txt"))) {
      SetErr(err, "缺 source.vocab.txt：%s", dir.c_str());
      return false;
    }
    try {
      sp_.reset(new ssentencepiece::Ssentencepiece(Join(dir, "source.vocab.txt"), 1));
    } catch (const std::exception &e) {
      SetErr(err, "加载 source.vocab.txt 失败：%s", e.what());
      return false;
    }

    if (!LoadPieces(Join(dir, "decode.pieces.txt"), &decode_pieces_, err)) return false;
    if (!LoadInt32(Join(dir, "encode.i32"), &encode_map_, err)) return false;
    if (encode_map_.empty() || decode_pieces_.empty()) {
      SetErr(err, "id 映射表为空：%s", dir.c_str());
      return false;
    }

    OrtSessionOptions *options = nullptr;
    if (api->CreateSessionOptions(&options) != nullptr) {
      SetErr(err, "CreateSessionOptions 失败");
      return false;
    }
    // ORT 的这些配置项几乎不可能失败（参数都是编译期常量），但 API 标了
    // warn_unused_result，必须显式丢弃，否则 -Werror 构建会挂。
    (void)api->SetIntraOpNumThreads(options, threads > 0 ? threads : kDefaultThreads);
    (void)api->SetInterOpNumThreads(options, 1);
    (void)api->SetSessionGraphOptimizationLevel(options, ORT_ENABLE_ALL);
    (void)api->SetSessionLogSeverityLevel(options, 3);

    const std::string encoder = Join(dir, "encoder_model_int8.onnx");
    const std::string decoder = Join(dir, "decoder_model_merged_int8.onnx");
    OrtStatus *status = api->CreateSession(env, encoder.c_str(), options, &enc_);
    if (!status) status = api->CreateSession(env, decoder.c_str(), options, &dec_);
    api->ReleaseSessionOptions(options);
    if (status) {
      SetErr(err, "创建 ONNX 会话失败：%s", api->GetErrorMessage(status));
      api->ReleaseStatus(status);
      return false;
    }

    if (!CacheEncoderIO(err) || !CacheDecoderIO(err)) return false;

    char summary[160];
    snprintf(summary, sizeof(summary), "%s(%s,eos=%d,start=%d,%d层/%d头)",
             BaseName(dir).c_str(), id_mode_.c_str(), eos_id_, start_id_, layers_, heads_);
    describe_ = summary;
    return true;
  }

  const std::string &Describe() const { return describe_; }

  bool Translate(const Nfkc &nfkc, const std::string &text, std::string *out,
                 std::string *err) {
    const std::string normalized = nfkc.Apply(text);

    std::vector<int32_t> source_ids;
    sp_->Encode(normalized, &source_ids);
    if (source_ids.empty()) {
      SetErr(err, "分不出 token");
      return false;
    }

    std::vector<int64_t> model_ids;
    model_ids.reserve(source_ids.size() + 1);
    for (int32_t id : source_ids) {
      if (id < 0 || static_cast<size_t>(id) >= encode_map_.size()) id = 0;
      model_ids.push_back(encode_map_[static_cast<size_t>(id)]);
    }
    model_ids.push_back(eos_id_);

    // VT_OPUS_TRACE=1 时打印编码结果 —— 排查「译文退化成通顺但无关的句子」时，
    // 第一步永远是确认送进 encoder 的 id 是不是期望的那些。
    const bool trace = getenv("VT_OPUS_TRACE") != nullptr;
    if (trace) {
      fprintf(stderr, "[vt_opus] %s src_ids:", describe_.c_str());
      for (size_t i = 0; i < source_ids.size(); ++i) fprintf(stderr, " %d", source_ids[i]);
      fprintf(stderr, "\n[vt_opus] %s model_ids:", describe_.c_str());
      for (size_t i = 0; i < model_ids.size(); ++i)
        fprintf(stderr, " %lld", static_cast<long long>(model_ids[i]));
      fprintf(stderr, "\n");
    }

    // encoder_attention_mask 既喂 encoder，也要在 decoder 的**每一步**再喂一次
    // （它挂在 decoder 的输入上），所以这张张量得活到解码循环结束 —— 不能提前释放。
    const int64_t src_len = static_cast<int64_t>(model_ids.size());
    OrtValue *ids_tensor = NewInt64Tensor(model_ids);
    OrtValue *mask_tensor = NewInt64Tensor(std::vector<int64_t>(model_ids.size(), 1));
    OrtValue *hidden = nullptr;
    ValueRelease release_ids{api_, ids_tensor};
    ValueRelease release_mask{api_, mask_tensor};
    ValueRelease release_hidden{api_, nullptr};
    if (ids_tensor == nullptr || mask_tensor == nullptr) {
      SetErr(err, "分配 encoder 输入失败");
      return false;
    }
    {
      const char *names[] = {encoder_ids_.c_str(), encoder_mask_.c_str()};
      const OrtValue *values[] = {ids_tensor, mask_tensor};
      const char *out_names[] = {encoder_hidden_.c_str()};
      OrtStatus *status = api_->Run(enc_, nullptr, names, values, 2,
                                    out_names, 1, &hidden);
      if (status) {
        SetErr(err, "encoder 推理失败：%s", api_->GetErrorMessage(status));
        api_->ReleaseStatus(status);
        return false;
      }
    }
    release_hidden.value = hidden;  // Run 之后才有值，交给守卫兜住失败的早退路径

    // —— 贪心逐步解码 ——
    // past 槽初值：encoder 侧给**真实源句长**、decoder 侧给 0（铁律 ②）
    std::vector<OrtValue *> past(slots_.size(), nullptr);
    for (size_t i = 0; i < past.size(); ++i) {
      past[i] = NewFloatZeros(slots_[i].encoder_side ? src_len : 0);
    }
    struct PastRelease {
      const OrtApi *api;
      std::vector<OrtValue *> *slots;
      ~PastRelease() {
        for (OrtValue *value : *slots) {
          if (value != nullptr) api->ReleaseValue(value);
        }
      }
    } release_past{api_, &past};

    std::vector<int32_t> decoded;
    int token = start_id_;
    for (int step = 0; step < kMaxNewTokens; ++step) {
      OrtValue *step_ids = NewInt64Tensor(std::vector<int64_t>{token});
      OrtValue *branch = branch_index_ >= 0 ? NewBoolTensor(step > 0) : nullptr;
      ValueRelease release_step{api_, step_ids};
      ValueRelease release_branch{api_, branch};

      std::vector<const char *> names;
      std::vector<const OrtValue *> values;
      names.reserve(3 + past.size() + 1);
      values.reserve(3 + past.size() + 1);
      names.push_back(decoder_ids_.c_str());
      values.push_back(step_ids);
      names.push_back(decoder_hidden_.c_str());
      values.push_back(hidden);
      names.push_back(decoder_mask_.c_str());
      values.push_back(mask_tensor);
      if (branch_index_ >= 0) {
        names.push_back(branch_name_.c_str());
        values.push_back(branch);
      }
      for (size_t i = 0; i < slots_.size(); ++i) {
        names.push_back(slots_[i].input.c_str());
        values.push_back(past[i]);
      }

      std::vector<OrtValue *> outputs(output_names_raw_.size(), nullptr);
      OrtStatus *status = api_->Run(dec_, nullptr, names.data(), values.data(),
                                    values.size(), output_names_raw_.data(),
                                    output_names_raw_.size(), outputs.data());
      if (status) {
        SetErr(err, "decoder 推理失败（第 %d 步）：%s", step,
               api_->GetErrorMessage(status));
        api_->ReleaseStatus(status);
        for (OrtValue *value : outputs) {
          if (value != nullptr) api_->ReleaseValue(value);
        }
        return false;
      }

      float *logits = nullptr;
      (void)api_->GetTensorMutableData(outputs[logits_index_],
                                       reinterpret_cast<void **>(&logits));
      for (int bad : bad_ids_) {
        if (bad >= 0 && bad < model_vocab_) logits[bad] = -INFINITY;
      }
      int best = 0;
      float best_value = logits[0];
      for (int i = 1; i < model_vocab_; ++i) {
        if (logits[i] > best_value) { best_value = logits[i]; best = i; }
      }

      const bool stop = (best == eos_id_);
      // 铁律 ③：encoder 槽只在第 0 步更新一次，之后从增量分支拿到的
      // present.*.encoder.* 是空张量，覆盖上去就把 cross-attention KV 毁了。
      for (size_t i = 0; i < slots_.size(); ++i) {
        OrtValue *present = outputs[slots_[i].output_index];
        if (step == 0 || !slots_[i].encoder_side) {
          if (past[i] != nullptr) api_->ReleaseValue(past[i]);
          past[i] = present;
        } else if (present != nullptr) {
          api_->ReleaseValue(present);
        }
      }
      if (outputs[logits_index_] != nullptr) api_->ReleaseValue(outputs[logits_index_]);

      if (stop) break;
      decoded.push_back(best);
      token = best;
    }

    // ⚠️ decoded 里装的是**模型 id**。这里直接用模型 id 索引 piece 表
    //    （导出时已经把两族 id 空间摊平），不去绕目标 spm 的原生 id —— 见 Detokenize。
    if (trace) {
      fprintf(stderr, "[vt_opus] %s decoded(%zu):", describe_.c_str(), decoded.size());
      for (size_t i = 0; i < decoded.size() && i < 24; ++i) fprintf(stderr, " %d", decoded[i]);
      fprintf(stderr, "\n");
    }

    *out = Detokenize(decoded);
    return true;
  }

 private:
  struct Slot {
    std::string input;
    size_t output_index;
    bool encoder_side;
  };

  static std::string BaseName(const std::string &dir) {
    const size_t slash = dir.find_last_of('/');
    return slash == std::string::npos ? dir : dir.substr(slash + 1);
  }

  void Release(OrtValue **value) {
    if (*value != nullptr) {
      api_->ReleaseValue(*value);
      *value = nullptr;
    }
  }
  OrtValue *NewInt64Tensor(const std::vector<int64_t> &values) {
    const int64_t shape[2] = {1, static_cast<int64_t>(values.size())};
    OrtValue *tensor = nullptr;
    if (api_->CreateTensorAsOrtValue(alloc_, shape, 2,
                                     ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64, &tensor)) {
      return nullptr;
    }
    int64_t *raw = nullptr;
    (void)api_->GetTensorMutableData(tensor, reinterpret_cast<void **>(&raw));
    memcpy(raw, values.data(), values.size() * sizeof(int64_t));
    return tensor;
  }

  OrtValue *NewFloatZeros(int64_t length) {
    const int64_t shape[4] = {1, heads_, length, head_dim_};
    OrtValue *tensor = nullptr;
    if (api_->CreateTensorAsOrtValue(alloc_, shape, 4,
                                     ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &tensor)) {
      return nullptr;
    }
    float *raw = nullptr;
    (void)api_->GetTensorMutableData(tensor, reinterpret_cast<void **>(&raw));
    memset(raw, 0, static_cast<size_t>(heads_ * length * head_dim_) * sizeof(float));
    return tensor;
  }

  OrtValue *NewBoolTensor(bool value) {
    const int64_t shape[1] = {1};
    OrtValue *tensor = nullptr;
    if (api_->CreateTensorAsOrtValue(alloc_, shape, 1,
                                     ONNX_TENSOR_ELEMENT_DATA_TYPE_BOOL, &tensor)) {
      return nullptr;
    }
    bool *raw = nullptr;
    (void)api_->GetTensorMutableData(tensor, reinterpret_cast<void **>(&raw));
    raw[0] = value;
    return tensor;
  }

  /// 词表文件：每行一个 piece（`<piece>\t<score>` 时只取 piece），行号即 id。
  bool LoadPieces(const std::string &path, std::vector<std::string> *out, std::string *err) {
    std::string blob;
    if (!ReadFile(path, &blob)) {
      SetErr(err, "读不到词表：%s", path.c_str());
      return false;
    }
    size_t start = 0;
    while (start < blob.size()) {
      size_t end = blob.find('\n', start);
      if (end == std::string::npos) end = blob.size();
      size_t tab = blob.find('\t', start);
      if (tab == std::string::npos || tab > end) tab = end;
      out->push_back(blob.substr(start, tab - start));
      start = end + 1;
    }
    if (!out->empty() && out->back().empty()) out->pop_back();
    if (out->empty()) {
      SetErr(err, "词表为空：%s", path.c_str());
      return false;
    }
    return true;
  }

  bool LoadInt32(const std::string &path, std::vector<int32_t> *out, std::string *err) {
    std::string blob;
    if (!ReadFile(path, &blob)) {
      SetErr(err, "读不到 id 映射表：%s", path.c_str());
      return false;
    }
    if (blob.size() % sizeof(int32_t) != 0) {
      SetErr(err, "id 映射表长度不是 4 的倍数：%s", path.c_str());
      return false;
    }
    out->resize(blob.size() / sizeof(int32_t));
    if (!out->empty()) memcpy(out->data(), blob.data(), blob.size());
    return true;
  }

  int CountIO(OrtSession *session, bool inputs, std::vector<std::string> *names) {
    size_t count = 0;
    OrtStatus *status = inputs ? api_->SessionGetInputCount(session, &count)
                               : api_->SessionGetOutputCount(session, &count);
    if (status) { api_->ReleaseStatus(status); return -1; }
    names->reserve(count);
    for (size_t i = 0; i < count; ++i) {
      char *name = nullptr;
      status = inputs ? api_->SessionGetInputName(session, i, alloc_, &name)
                      : api_->SessionGetOutputName(session, i, alloc_, &name);
      if (status) { api_->ReleaseStatus(status); return -1; }
      names->push_back(name);
      alloc_->Free(alloc_, name);
    }
    return static_cast<int>(count);
  }

  bool CacheEncoderIO(std::string *err) {
    if (CountIO(enc_, true, &encoder_inputs_) < 0) {
      SetErr(err, "读不到 encoder 输入名");
      return false;
    }
    if (CountIO(enc_, false, &encoder_outputs_) < 0) {
      SetErr(err, "读不到 encoder 输出名");
      return false;
    }
    for (const auto &name : encoder_inputs_) {
      if (name.find("attention_mask") != std::string::npos) encoder_mask_ = name;
      else if (name.find("input_ids") != std::string::npos) encoder_ids_ = name;
    }
    encoder_hidden_ = encoder_outputs_.empty() ? "last_hidden_state" : encoder_outputs_[0];
    return true;
  }

  /// 槽位命名：输入 `past_key_values.<层>.{encoder|decoder}.{key|value}`，
  /// 输出 `present.<层>.{encoder|decoder}.{key|value}` —— 直接换前缀对应。
  bool CacheDecoderIO(std::string *err) {
    if (CountIO(dec_, true, &decoder_inputs_) < 0) {
      SetErr(err, "读不到 decoder 输入名");
      return false;
    }
    if (CountIO(dec_, false, &decoder_outputs_) < 0) {
      SetErr(err, "读不到 decoder 输出名");
      return false;
    }
    std::map<std::string, size_t> output_index;
    for (size_t i = 0; i < decoder_outputs_.size(); ++i) {
      output_index[decoder_outputs_[i]] = i;
      if (decoder_outputs_[i] == "logits") logits_index_ = static_cast<int>(i);
    }

    for (const auto &name : decoder_inputs_) {
      const std::string prefix = "past_key_values.";
      if (name.compare(0, prefix.size(), prefix) != 0) {
        if (name == "use_cache_branch") { branch_index_ = 1; branch_name_ = name; }
        else if (name.find("attention_mask") != std::string::npos) decoder_mask_ = name;
        else if (name.find("input_ids") != std::string::npos) decoder_ids_ = name;
        else if (name.find("encoder_hidden_states") != std::string::npos) decoder_hidden_ = name;
        continue;
      }
      const std::string present = "present." + name.substr(prefix.size());
      auto found = output_index.find(present);
      if (found == output_index.end()) {
        SetErr(err, "decoder 输出里找不到 %s", present.c_str());
        return false;
      }
      Slot slot;
      slot.input = name;
      slot.output_index = found->second;
      slot.encoder_side = name.find(".encoder.") != std::string::npos;
      slots_.push_back(std::move(slot));
    }
    if (slots_.empty()) {
      SetErr(err, "decoder 没有任何 past_key_values 槽");
      return false;
    }
    if (logits_index_ < 0) {
      SetErr(err, "decoder 输出里没有 logits");
      return false;
    }
    // Run 的输出名数组要稳定，提前拍平成 C 字符串
    output_names_raw_.reserve(decoder_outputs_.size());
    for (const auto &name : decoder_outputs_) output_names_raw_.push_back(name.c_str());
    if (decoder_ids_.empty() || decoder_hidden_.empty() || decoder_mask_.empty()) {
      SetErr(err, "decoder 的 input_ids / encoder_hidden_states / encoder_attention_mask 没找全");
      return false;
    }
    return true;
  }

  /// 把模型输出的 id 还原成文本。
  ///
  /// 直接用**模型 id** 索引 piece 表（导出时已把两族 id 空间摊平），不绕目标 spm 的
  /// 原生 id —— 模型可能输出只存在于**源**侧词表的 piece，那种 id 在 target.spm 里
  /// 没有对应项，绕过去只能退回 `<unk>`，译文里就凭空冒出 `<unk>` 字面量
  /// （实测：`Chen Xiaoming` → `Chen <unk>iaoming`）。
  ///
  /// 刻意**不用 ssentencepiece 的 Decode**：它对结果无条件 `substr(1)`，
  /// 而目标语是中文/日文时首 token 并不以 `▁` 开头，那一下会把首字节砍掉、
  /// 直接把 UTF-8 打坏。
  std::string Detokenize(const std::vector<int32_t> &model_ids) const {
    std::string out;
    for (int32_t id : model_ids) {
      if (id < 0 || static_cast<size_t>(id) >= decode_pieces_.size()) continue;
      const std::string &piece = decode_pieces_[static_cast<size_t>(id)];
      if (piece.empty()) continue;
      // sentencepiece 的 decode 会丢掉控制符，这里必须照做，
      // 否则 `</s>` / `<unk>` 会以字面量形式混进译文。
      if (piece.size() >= 3 && piece.front() == '<' && piece.back() == '>') {
        if (piece == "<unk>" || piece == "<s>" || piece == "</s>" || piece == "<pad>") {
          continue;
        }
        // `<0xNN>` 是字节回退，还原成原始字节
        if (piece.size() == 6 && piece.compare(0, 3, "<0x") == 0) {
          out += static_cast<char>(strtol(piece.c_str() + 3, nullptr, 16));
          continue;
        }
      }
      if (piece.compare(0, 3, kSpaceMark) == 0) {
        out += ' ';
        out.append(piece, 3, std::string::npos);
      } else {
        out += piece;
      }
    }
    if (!out.empty() && out[0] == ' ') out.erase(0, 1);
    return out;
  }

  const OrtApi *api_ = nullptr;
  OrtAllocator *alloc_ = nullptr;
  OrtSession *enc_ = nullptr;
  OrtSession *dec_ = nullptr;

  std::vector<std::string> encoder_inputs_, encoder_outputs_;
  std::vector<std::string> decoder_inputs_, decoder_outputs_;
  std::vector<const char *> output_names_raw_;
  std::string encoder_ids_, encoder_mask_, encoder_hidden_;
  std::string decoder_ids_, decoder_hidden_, decoder_mask_;
  std::string branch_name_;
  int branch_index_ = -1;
  int logits_index_ = -1;

  std::unique_ptr<ssentencepiece::Ssentencepiece> sp_;
  std::vector<std::string> decode_pieces_;
  std::vector<int32_t> encode_map_;

  std::vector<Slot> slots_;
  std::vector<int> bad_ids_;
  std::string id_mode_, describe_;
  int eos_id_ = 0, start_id_ = 0, pad_id_ = 0;
  int heads_ = 0, head_dim_ = 0, layers_ = 0, model_vocab_ = 0;
};

}  // namespace

// ---------------------------------------------------------------- 对外句柄

struct VtOpus {
  const OrtApi *api = nullptr;
  OrtEnv *env = nullptr;
  Nfkc nfkc;
  std::string directory;                                    // 包根目录（按需装载时要用）
  int threads = kDefaultThreads;
  std::map<std::string, std::vector<std::string>> routes;   // 语向 -> 单包列表
  std::map<std::string, std::unique_ptr<Route>> models;     // 单包 -> 模型（按需装载）
  std::vector<std::string> missing;                         // 磁盘上就缺的单包
  std::mutex mutex;
};

namespace {

/// routes.txt：一行一个语向，制表符分隔「语向 + 逗号分隔的单包列表」。
bool LoadRoutes(const std::string &path,
                std::map<std::string, std::vector<std::string>> *routes) {
  std::string blob;
  if (!ReadFile(path, &blob)) return false;
  size_t start = 0;
  while (start < blob.size()) {
    size_t end = blob.find('\n', start);
    if (end == std::string::npos) end = blob.size();
    const std::string line = blob.substr(start, end - start);
    start = end + 1;
    const size_t tab = line.find('\t');
    if (tab == std::string::npos || tab == 0) continue;
    std::vector<std::string> hops;
    size_t cursor = tab + 1;
    while (cursor <= line.size()) {
      size_t comma = line.find(',', cursor);
      if (comma == std::string::npos) comma = line.size();
      const std::string hop = line.substr(cursor, comma - cursor);
      if (!hop.empty()) hops.push_back(hop);
      if (comma >= line.size()) break;
      cursor = comma + 1;
    }
    if (!hops.empty()) (*routes)[line.substr(0, tab)] = hops;
  }
  return !routes->empty();
}

std::string Key(const char *source, const char *target) {
  return std::string(source ? source : "") + "-" + (target ? target : "");
}

void CopyOut(const std::string &text, char *error, int capacity) {
  if (!error || capacity <= 0) return;
  snprintf(error, static_cast<size_t>(capacity), "%s", text.c_str());
}

/// 这个单包在磁盘上是否完整（两份 ONNX 都在）。
bool HopPresent(const std::string &dir, const std::string &hop) {
  const std::string hop_dir = Join(dir, "routes/" + hop);
  return IsFile(Join(hop_dir, "encoder_model_int8.onnx")) &&
         IsFile(Join(hop_dir, "decoder_model_merged_int8.onnx"));
}

/// 按需装载一个单包；已装载的直接返回。
///
/// ### 为什么不在 vt_opus_load 里一次装完
/// OPUS 是「一个语向一个模型」，默认端侧包带 3 个单包共约 548 MiB，
/// 而**一次会话只会用到其中一两个**（zh→en 只碰 zh-en 的 147.6 MiB）。
/// ORT 的 `CreateSession` 会把权重读进内存（不像 llama.cpp 用 mmap），
/// 所以在装载期一次读完三个包的代价是「每次开始翻译都先卡几秒 + 常驻 548 MiB」——
/// 在 iPhone 4 那代内存的机器上（本项目目标机 iPad mini 4 / A8 / 2 GB）尤其难受。
///
/// 推迟到真要翻译的那一刻就只剩当前语向那一份。形态与 Qwen 路径一致：
/// 第一句明显慢（那边是 prefill，这边是建会话），之后每句都很快。
bool EnsureHop(VtOpus *opus, const std::string &hop, std::string *reason) {
  if (opus->models.count(hop)) return true;

  if (!HopPresent(opus->directory, hop)) {
    if (std::find(opus->missing.begin(), opus->missing.end(), hop) == opus->missing.end()) {
      opus->missing.push_back(hop);
    }
    *reason = hop + " 包缺失或不完整";
    return false;
  }

  std::unique_ptr<Route> route(new Route());
  if (!route->Load(opus->api, opus->env, Join(opus->directory, "routes/" + hop),
                   opus->threads, reason)) {
    *reason = hop + "：" + *reason;
    return false;
  }
  opus->models[hop] = std::move(route);
  return true;
}

}  // namespace

extern "C" {

VtOpus *vt_opus_load(const char *directory, int threads, char *error, int error_capacity) {
  if (!directory || !*directory) {
    CopyOut("包目录为空", error, error_capacity);
    return nullptr;
  }
  const OrtApi *api = vt::Api();
  if (!api) {
    CopyOut("这个平台上拿不到 ONNX Runtime", error, error_capacity);
    return nullptr;
  }

  const std::string dir = directory;
  std::string reason;
  std::unique_ptr<VtOpus> opus(new VtOpus());
  opus->api = api;
  opus->directory = dir;
  opus->threads = threads > 0 ? threads : kDefaultThreads;

  if (!opus->nfkc.Load(Join(dir, "norm/nfkc.bin"), &reason)) {
    CopyOut(reason, error, error_capacity);
    return nullptr;
  }
  if (!LoadRoutes(Join(dir, "routes.txt"), &opus->routes)) {
    CopyOut("读不到 routes.txt（用 scripts/export-opus-package.py 产出）", error, error_capacity);
    return nullptr;
  }
  if (api->CreateEnv(ORT_LOGGING_LEVEL_ERROR, "vt-opus", &opus->env) != nullptr) {
    CopyOut("创建 ORT 环境失败", error, error_capacity);
    return nullptr;
  }

  // 逐单包**只登记缺失**，不建 ONNX 会话 —— 会话推迟到 vt_opus_translate
  // 里按需建，见 EnsureHop 的说明。
  // 缺一个包不算失败：受影响的语向由 vt_opus_supports 返回 0，
  // 由调用方回落到通用模型（这就是「智能混合」模式在没有 OPUS 包时的行为）。
  for (const auto &entry : opus->routes) {
    for (const auto &hop : entry.second) {
      if (!HopPresent(dir, hop) &&
          std::find(opus->missing.begin(), opus->missing.end(), hop) == opus->missing.end()) {
        opus->missing.push_back(hop);
      }
    }
  }
  return opus.release();
}

int vt_opus_supports(const VtOpus *opus, const char *source, const char *target) {
  if (!opus) return 0;
  auto found = opus->routes.find(Key(source, target));
  if (found == opus->routes.end()) return 0;
  // 只看装载期登记下来的 missing，不再探磁盘 —— supports 会被构造期和设置页
  // 频繁调用，不该带 I/O。（「文件在不在」在装载期就定了，中途不会变。）
  for (const auto &hop : found->second) {
    if (std::find(opus->missing.begin(), opus->missing.end(), hop) != opus->missing.end()) {
      return 0;
    }
  }
  return 1;
}

char *vt_opus_translate(VtOpus *opus, const char *text, const char *source,
                        const char *target, char *error, int error_capacity) {
  if (!opus || !text) {
    CopyOut("参数为空", error, error_capacity);
    return nullptr;
  }
  std::lock_guard<std::mutex> guard(opus->mutex);

  auto found = opus->routes.find(Key(source, target));
  if (found == opus->routes.end()) {
    CopyOut(Key(source, target) + " 不在 OPUS 接线里", error, error_capacity);
    return nullptr;
  }

  std::string current = text;
  for (const auto &hop : found->second) {
    std::string reason;
    if (!EnsureHop(opus, hop, &reason)) {
      CopyOut(found->first + " 需要 " + reason, error, error_capacity);
      return nullptr;
    }
    std::string next;
    if (!opus->models[hop]->Translate(opus->nfkc, current, &next, &reason)) {
      CopyOut(hop + "：" + reason, error, error_capacity);
      return nullptr;
    }
    current = next;
  }

  char *out = static_cast<char *>(malloc(current.size() + 1));
  if (!out) {
    CopyOut("内存不足", error, error_capacity);
    return nullptr;
  }
  memcpy(out, current.c_str(), current.size() + 1);
  return out;
}

char *vt_opus_describe(const VtOpus *opus) {
  if (!opus) return nullptr;
  std::string text;
  // 单包是**按需装载**的，所以「已装载」在第一次翻译之前必然是空的 ——
  // 那种状态下给出接线表，比一句「未加载」有用得多。
  for (const auto &entry : opus->models) {
    if (!text.empty()) text += "、";
    text += entry.second->Describe();
  }
  if (text.empty()) {
    text = "接线 " + std::to_string(opus->routes.size()) + " 个语向，单包按需装载";
  }
  if (!opus->missing.empty()) {
    text += "；缺包：";
    for (size_t i = 0; i < opus->missing.size(); ++i) {
      if (i) text += ",";
      text += opus->missing[i];
    }
  }
  char *out = static_cast<char *>(malloc(text.size() + 1));
  if (!out) return nullptr;
  memcpy(out, text.c_str(), text.size() + 1);
  return out;
}

void vt_opus_free_string(char *text) { free(text); }

void vt_opus_close(VtOpus *opus) {
  if (!opus) return;
  opus->models.clear();
  if (opus->env && opus->api) opus->api->ReleaseEnv(opus->env);
  delete opus;
}

}  // extern "C"
