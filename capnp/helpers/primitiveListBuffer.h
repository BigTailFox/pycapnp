#pragma once

#include <capnp/dynamic.h>
#include <capnp/schema.h>

#include <cstddef>

namespace pycapnp {

struct PrimitiveListElementInfo {
  size_t elementSize;
  const char* format;
  bool eligible;
};

PrimitiveListElementInfo getPrimitiveListElementInfo(capnp::Type elementType);

bool getPrimitiveListReaderBuffer(
    capnp::DynamicList::Reader list,
    capnp::Type elementType,
    const void** outPtr,
    size_t* outByteLen,
    size_t* outElementCount);

bool getPrimitiveListBuilderBuffer(
    capnp::DynamicList::Builder list,
    capnp::Type elementType,
    void** outPtr,
    size_t* outByteLen,
    size_t* outElementCount);

bool setPrimitiveListBuilderFromBuffer(
    capnp::DynamicList::Builder list,
    capnp::Type elementType,
    const void* src,
    size_t srcByteLen);

}  // namespace pycapnp
