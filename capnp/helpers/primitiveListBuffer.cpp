#include "capnp/helpers/primitiveListBuffer.h"

#include <capnp/any.h>
#include <capnp/list.h>

#include <cstdint>
#include <cstring>

namespace pycapnp {
namespace {

bool isHostLittleEndian() {
  const uint16_t one = 1;
  return *reinterpret_cast<const uint8_t*>(&one) == 1;
}

bool rawBytesForReader(
    capnp::DynamicList::Reader list,
    capnp::Type elementType,
    const void** outPtr,
    size_t* outByteLen,
    size_t* outElementCount) {
  auto info = getPrimitiveListElementInfo(elementType);
  if (!info.eligible) {
    return false;
  }

  capnp::AnyList::Reader anyList = list.as<capnp::AnyList>();
  auto raw = anyList.getRawBytes();
  const size_t elementCount = list.size();
  const size_t expectedByteLen = elementCount * info.elementSize;

  if (raw.size() != expectedByteLen) {
    return false;
  }

  *outPtr = raw.begin();
  *outByteLen = raw.size();
  *outElementCount = elementCount;
  return true;
}

}  // namespace

PrimitiveListElementInfo getPrimitiveListElementInfo(capnp::Type elementType) {
  if (!isHostLittleEndian()) {
    return {0, nullptr, false};
  }

  switch (elementType.which()) {
    case capnp::schema::Type::INT8:
      return {1, "<b", true};
    case capnp::schema::Type::INT16:
      return {2, "<h", true};
    case capnp::schema::Type::INT32:
      return {4, "<i", true};
    case capnp::schema::Type::INT64:
      return {8, "<q", true};
    case capnp::schema::Type::UINT8:
      return {1, "<B", true};
    case capnp::schema::Type::UINT16:
      return {2, "<H", true};
    case capnp::schema::Type::UINT32:
      return {4, "<I", true};
    case capnp::schema::Type::UINT64:
      return {8, "<Q", true};
    case capnp::schema::Type::FLOAT32:
      return {4, "<f", true};
    case capnp::schema::Type::FLOAT64:
      return {8, "<d", true};
    default:
      return {0, nullptr, false};
  }
}

bool getPrimitiveListReaderBuffer(
    capnp::DynamicList::Reader list,
    capnp::Type elementType,
    const void** outPtr,
    size_t* outByteLen,
    size_t* outElementCount) {
  return rawBytesForReader(list, elementType, outPtr, outByteLen, outElementCount);
}

bool getPrimitiveListBuilderBuffer(
    capnp::DynamicList::Builder list,
    capnp::Type elementType,
    void** outPtr,
    size_t* outByteLen,
    size_t* outElementCount) {
  const void* readPtr = nullptr;
  if (!rawBytesForReader(list.asReader(), elementType, &readPtr, outByteLen, outElementCount)) {
    return false;
  }

  *outPtr = const_cast<void*>(readPtr);
  return true;
}

bool setPrimitiveListBuilderFromBuffer(
    capnp::DynamicList::Builder list,
    capnp::Type elementType,
    const void* src,
    size_t srcByteLen) {
  void* dst = nullptr;
  size_t byteLen = 0;
  size_t elementCount = 0;

  if (!getPrimitiveListBuilderBuffer(list, elementType, &dst, &byteLen, &elementCount)) {
    return false;
  }
  if (byteLen != srcByteLen) {
    return false;
  }
  if (srcByteLen > 0) {
    memcpy(dst, src, srcByteLen);
  }
  return true;
}

}  // namespace pycapnp
