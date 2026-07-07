# Primitive `List(T)` Buffer-Protocol Fast Path (Setter + Reader)

Design plan for zero-copy / memcpy-speed access to Cap'n Proto primitive list fields
in pycapnp. Motivated by [capnproto/pycapnp#399](https://github.com/capnproto/pycapnp/issues/399).

## Summary

Today, assigning `msg.float32List = arr` only accepts Python `list` / `tuple` and
boxes every element. Reading `msg.float32List[i]` materializes scalar Python objects.
For numerical workloads this dominates serialization time and holds the GIL.

This plan adds:

1. **Setter fast path** — assign any buffer-protocol object (`memoryview`, `array.array`,
   `numpy.ndarray`, …) to a primitive list field via `init` + `memcpy`.
2. **Reader buffer export** — accessing `msg.<scalarListField>` returns an object that
   **is** a buffer-protocol exporter (usable as `memoryview(...)`, `numpy.frombuffer(...)`,
   etc.) for eligible primitive list types.

**Non-goals (v1):** `List(Bool)` bitfield layout, big-endian hosts, non-contiguous /
mis-typed buffers. These **fall through to the existing slow path** (or current error
behavior) without attempting a fast path.

## User-Facing API (Target)

### Write

```python
import numpy as np

msg.samples = np.array([0, 1, 2, 3], dtype=np.float32)
msg.samples = memoryview(arr)          # no numpy dependency required
msg.samples = array.array("h", data)   # List(Int16)
msg.samples = [1, 2, 3]                # unchanged slow path
```

### Read

```python
lst = reader.samples                   # buffer-protocol object (primitive lists)
mv = memoryview(reader.samples)        # zero-copy view of list storage
arr = np.frombuffer(reader.samples, dtype=np.float32)  # optional numpy

# Existing indexing still works via slow path delegation:
assert reader.samples[0] == 0          # scalar boxing, unchanged semantics
assert len(reader.samples) == 4
```

For **non-eligible** lists (`List(Text)`, `List(Data)`, `List(Struct)`, `List(Bool)`,
`List(Enum)`, …), `reader.samples` continues to return `_DynamicListReader` with
today's behavior.

## Current Behavior (Baseline)

| Operation | Path | Copies? |
|-----------|------|---------|
| `msg.float32List = python_list` | `module.pyx` / `_setDynamicField` → `init` + per-element `_set` | Per-element boxing |
| `msg.float32List = np.ndarray` | Rejected (`unsupported type` or wrong branch) | N/A |
| `msg.float32List = memoryview` | Hits `_setMemoryview` (Data path) → type mismatch | N/A |
| `reader.float32List` | `_DynamicListReader` wrapper | Container: no |
| `reader.float32List[i]` | `to_python_reader` → Python `float` | Scalar boxing |
| `list(reader.float32List)` | Iteration + scalar materialization | Yes |

Relevant code today:

- Setter: `capnp/lib/capnp.pyx` — `_setDynamicField` / `_setDynamicFieldStatic` (~824)
- List fill: `_from_list` (~1077)
- Typed setter template: `capnp/templates/module.pyx` — `_set_{{field.name}}(self, list value)`
- Memoryview setter (Data only): `_setMemoryview` (~775)
- Reader list wrapper: `_DynamicListReader` (~412)
- Buffer exporter precedent: `_BorrowedBufferView` + `_memoryview_borrowing` (~1182)

## Eligibility Rules (Fast Path Gate)

A field qualifies **only if all** checks pass. Otherwise **fall through silently** to
the existing slow path (or existing errors).

### Schema

- Field type is `List(T)`.
- Element type `T` is one of:

  | Cap'n Proto type | Element size | Buffer `format` (LE host) |
  |------------------|-------------|---------------------------|
  | `Int8`           | 1           | `b`                       |
  | `Int16`          | 2           | `h`                       |
  | `Int32`          | 4           | `i`                       |
  | `Int64`          | 8           | `q`                       |
  | `UInt8`          | 1           | `B`                       |
  | `UInt16`         | 2           | `H`                       |
  | `UInt32`         | 4           | `I`                       |
  | `UInt64`         | 8           | `Q`                       |
  | `Float32`        | 4           | `f`                       |
  | `Float64`        | 8           | `d`                       |

- **Excluded:** `Bool` (bitfield), `Text`, `Data`, `Struct`, `Enum`, `Interface`,
  nested `List`, `AnyPointer`, …

### Buffer (setter input)

- `PyObject_CheckBuffer(value)` is true.
- `PyObject_GetBuffer(..., PyBUF_CONTIG_RO)` succeeds.
- `buf.len == count * element_size` where `count = buf.len // element_size`
  (reject trailing partial element → fall through).
- Host is **little-endian** (`sys.byteorder == "little"`) — otherwise fall through.
- Optional strictness (recommended v1): require `buf.itemsize == element_size` when
  NumPy/array sets non-zero itemsize; if mismatch → fall through.

### Message state (setter)

- Field is unset **or** will be (re)initialized: call `thisptr.init(field, count)`
  before memcpy (same as today's list assignment).

## Architecture Overview

```text
                    ┌─────────────────────────────────────┐
  assignment        │  _try_set_primitive_list_from_buf   │
  ───────────────►  │  (schema gate + buffer validate)    │
                    └──────────────┬──────────────────────┘
                                   │ success: init + memcpy
                                   │ failure: return False → slow path

  field read          ┌─────────────────────────────────────┐
  ───────────────►    │  _PrimitiveScalarListView           │
  (eligible List(T))  │  • __getbuffer__ / __releasebuffer__│
                      │  • __len__ → element count          │
                      │  • __getitem__ → slow scalar read   │
                      └──────────────┬──────────────────────┘
                                     │ pins owner via exporter
                                     ▼
                      _BorrowedBufferView (extended metadata)
```

### Why a C++ helper

Cap'n Proto stores primitive list elements in segment memory with a fixed little-endian
layout, but exposing a stable raw pointer from `DynamicList::Builder/Reader` requires
schema-driven dispatch (`List<T>` templates). A small C++ translation unit keeps
`capnp.pyx` readable and mirrors existing helpers (`capabilityHelper.cpp`,
`PyCustomMessageBuilder.cpp`).

Suggested files:

```text
capnp/helpers/primitiveListBuffer.h
capnp/helpers/primitiveListBuffer.cpp
```

Suggested API (C++, namespace `pycapnp`):

```cpp
struct PrimitiveListElementInfo {
  size_t elementSize;
  const char* format;   // PEP 3118 single-char format, native LE
  bool eligible;        // false for Bool, non-primitives, etc.
};

PrimitiveListElementInfo getPrimitiveListElementInfo(
    capnp::SchemaType elementType);

// Reader: expose contiguous bytes if layout allows
bool getPrimitiveListReaderBuffer(
    capnp::DynamicList::Reader list,
    capnp::SchemaType elementType,
    const void** outPtr,
    size_t* outByteLen,
    size_t* outElementCount);

// Builder: after caller init()'d the list to count elements
bool setPrimitiveListBuilderFromBuffer(
    capnp::DynamicList::Builder list,
    capnp::SchemaType elementType,
    const void* src,
    size_t srcByteLen);
```

Implementation note: use a `switch (elementType.which())` over
`capnp::schema::Type` and typed `list.as<List<capnp::Int16>>()` (etc.) accessors from
Cap'n Proto C++ API to obtain `begin()` / element stride. If a future Cap'n Proto
version does not expose contiguous storage for a type, return `false` (fall through).

Wire-format note (from #399): for eligible numeric types on LE hosts, list bytes are
bit-identical to a packed numpy/`array.array` buffer of the same dtype.

## Python / Cython Integration

### 1. Setter hook (critical ordering)

In **`_setDynamicField`** and **`_setDynamicFieldStatic`**, insert **before**
`isinstance(value, BuiltinsMemoryview)` / `_setMemoryview`:

```python
if _try_set_primitive_list_from_buffer(thisptr, field, value, parent):
    return
# existing branches: bytes, memoryview (Data), list, ...
```

`_setDynamicFieldWithField` gets the same branch (it already has schema via
`_StructSchemaField`).

Why ordering matters: today `memoryview(arr)` on a list field incorrectly enters the
Data fast path and raises `Value type mismatch [6 == 8]`.

#### `_try_set_primitive_list_from_buffer(...) -> bool`

Steps:

1. Resolve field schema:
   - `_setDynamicFieldStatic`: `parent.schema` + field name →
     `StructSchema.getFieldByName` → `field.getType()`.
   - `_setDynamicFieldWithField`: `field.thisptr.getType()` directly.
   - `_setDynamicField` (list index): not in v1 scope (see below).
2. If not `List(T)` or `getPrimitiveListElementInfo` says `eligible == false`:
   **return False**.
3. If `sys.byteorder != "little"`: **return False**.
4. `PyObject_GetBuffer` + validate length / contiguity.
5. `count = buf.len // element_size`; `thisptr.init(field, count)`.
6. Obtain `DynamicList::Builder` from returned `DynamicValue.Builder`.
7. Call `setPrimitiveListBuilderFromBuffer`.
8. `PyBuffer_Release`; **return True**.

On any failure: release buffer if acquired, **return False** (no new exception type in
v1 unless the slow path would also fail).

### 2. Typed schema setters (`module.pyx`)

Change generated primitive-list setter from:

```jinja
cpdef _set_{{field.name}}(self, list value):
```

to:

```jinja
cpdef _set_{{field.name}}(self, object value):
    if not _try_set_typed_primitive_list_from_buffer(self, "{{field.name}}", value):
        if not isinstance(value, list):
            raise TypeError(...)
        ...  # existing init + loop
```

For struct / text / enum list fields, keep `list value` signature unchanged.

Alternatively, call the shared `_try_set_primitive_list_from_buffer(self.thisptr, ...)`
from typed setters for numeric lists only (codegen knows `field.sub_type`).

### 3. Reader: `_PrimitiveScalarListView`

New `@cython.internal` class wrapping:

- `owner` — `_DynamicStructReader` or `_DynamicStructBuilder` (pins message lifetime)
- `C_DynamicList.Reader` or `Builder` pointer (builder view is read/write)
- `elementType` — `SchemaType` or cached `PrimitiveListElementInfo`
- `readonly` flag

#### Construction sites

Replace bare `_DynamicListReader()._init(...)` when returning `TYPE_LIST` **if**
eligible:

| Call site | How to get element schema |
|-----------|---------------------------|
| `to_python_reader` / `to_python_builder` | Needs list element type — pass `ListSchema` or resolve from parent + field name (see gap below) |
| Typed `_get_{{field.name}}` | Codegen knows element type; call typed factory |
| `_DynamicListReader._get(i)` | Stays scalar slow path; no change |

**Gap:** `to_python_reader(..., parent)` for `TYPE_LIST` does not know the field name.
Options (pick one in implementation):

- **A (recommended):** Add overload `to_python_reader(val, parent, field_schema=None)`.
  Struct `_get(field)` passes `StructSchema.getFieldByName(field).getType()`.
- **B:** Store `(parent, field_name)` on struct getters only; list factory resolves lazily.
- **C:** Attach `ListSchema` to `_DynamicListReader` at creation from typed codegen only;
  dynamic `_DynamicStructReader._get` uses option A.

#### Buffer protocol surface

Implement `__getbuffer__` / `__releasebuffer__` on `_PrimitiveScalarListView` (or delegate
to a dedicated exporter object):

```python
buffer->buf   = <pointer to first element>
buffer->len   = element_count * element_size   # bytes
buffer->itemsize = element_size
buffer->format = "<f" / "<h" / ...              # explicit LE prefix (PEP 3118)
buffer->ndim  = 1
buffer->shape = [element_count]
buffer->strides = [element_size]
buffer->readonly = (owner is Reader)
```

Use `<` prefix in `format` so numpy interprets as little-endian regardless of native
`array` format chars. On LE hosts this matches wire bytes.

Reuse lifetime model from `_BorrowedBufferView`:

```text
memoryview ──► exporter ──► _PrimitiveScalarListView ──► struct reader/builder ──► message
```

Extend `_BorrowedBufferView` **or** add `_ScalarListBufferExporter` sibling if full
PEP 3118 metadata is required (current `_BorrowedBufferView` only sets base pointer +
length via `PyBuffer_FillInfo` — insufficient for `numpy.frombuffer` without explicit
dtype).

#### Preserve existing sequence API

`_PrimitiveScalarListView` should also implement:

- `__len__` → element count (C++ `list.size()`)
- `__getitem__(i)` → delegate to existing `to_python_reader(list[i], owner)` (slow scalar)
- `__repr__` → `<capnp primitive list view T=N readonly/writable>`

This keeps backward compatibility for code using indexing while enabling buffer export
for numeric code paths.

Optional convenience (not required v1):

```python
reader.get_list_as_view("samples")  # alias; same object as reader.samples when eligible
```

### 4. Builder list as buffer (read/write view)

For `_DynamicStructBuilder`, eligible `msg.samples` access should return
`_PrimitiveScalarListView` with `readonly=False` **after** the field is initialized.

Edge cases:

- **Uninitialized list:** same as Data — expose zero-length buffer or empty view;
  `len == 0`; assignment via buffer setter performs `init(count)`.
- **Outstanding views + mutation:** document same hazard as `get_data_as_view` — do not
  re-init / resize the list while a view is alive.

## Corner Cases → Slow Path (Explicit)

| Case | v1 behavior |
|------|-------------|
| `List(Bool)` | Fast path declined; use Python list / indexing |
| Big-endian host | Fast path declined |
| Non-contiguous buffer | `PyObject_GetBuffer` fails or validate fails → slow path |
| Wrong `itemsize` / dtype width | Fall through |
| `List(Enum)` assigned buffer | Fall through |
| Partial trailing bytes (`len % elem_size != 0`) | Fall through |
| `memoryview` to list field | **Must** hit list fast path, not Data path |

## Files to Change

| File | Change |
|------|--------|
| `capnp/helpers/primitiveListBuffer.h` | **New** — C++ declarations |
| `capnp/helpers/primitiveListBuffer.cpp` | **New** — schema dispatch + pointer access |
| `setup.py` / build backend | Link new `.cpp` |
| `capnp/lib/capnp.pyx` | `_try_set_*`, `_PrimitiveScalarListView`, hook `_setDynamicField*`, extend `to_python_reader` |
| `capnp/lib/capnp.pxd` | Export cdef hooks for `module.pyx` if needed |
| `capnp/templates/module.pyx` | Typed setter/read getter for numeric lists |
| `test/test_primitive_list_buffer.py` | **New** — setter + reader tests |
| `CHANGELOG.md` | Feature note (non-breaking additive) |

## Testing Plan

### Setter

- [ ] `float32List = array.array('f', [...])` round-trip via `to_bytes` / read back
- [ ] `int16List = memoryview(...)` length 8192 — benchmark vs `tolist()` (optional perf gate)
- [ ] Python `list` assignment still works (regression)
- [ ] `boolList = [True, False]` unchanged (slow path)
- [ ] `memoryview` on list field no longer raises Data type mismatch
- [ ] Wrong-size buffer falls through to slow path or clear `TypeError` (define one)
- [ ] Typed + dynamic struct paths both covered

### Reader buffer export

- [ ] `memoryview(reader.float32List)` — zero-copy, correct length
- [ ] `bytes(memoryview(...))` matches serialized list payload bytes (LE)
- [ ] `reader.float32List[0]` still returns Python scalar
- [ ] `len(reader.float32List)` without copying all elements
- [ ] View pins message (`weakref` on parent, same pattern as `test_get_data_view.py`)
- [ ] `List(Text)` still returns `_DynamicListReader`, not buffer view
- [ ] Uninitialized list → empty buffer, `len == 0`

### Optional (if numpy in test extras)

- [ ] `np.frombuffer(reader.samples, dtype=np.float32)` shape/count
- [ ] Round-trip `arr → msg → memoryview → np.frombuffer` equality

## Performance Expectation

From #399 (8192 × `Int16`, including `to_bytes()`):

| Path | Target |
|------|--------|
| `tolist()` (baseline) | ~0.31 ms |
| `init` + Python loop | ~1.27 ms (worse) |
| `Data ← tobytes()` | ~0.002 ms |
| **New list fast path** | Same order as `Data` (~memcpy-dominated) |

## Migration / Compatibility

- **Non-breaking:** Python list assignment unchanged.
- **Additive:** eligible list fields gain buffer export on read; `isinstance(x, _DynamicListReader)` may become false for numeric lists — document that callers should use buffer protocol probes (`memoryview(x)` / `PyObject_CheckBuffer`) instead of concrete class checks.
- **No numpy dependency** in core package.

## Implementation Phases

### Phase 1 — C++ helper + setter (highest ROI)

1. Implement `primitiveListBuffer.cpp` with unit-tested C++ or pytest integration tests.
2. Wire `_try_set_primitive_list_from_buffer` before `_setMemoryview`.
3. Update typed numeric list setters in `module.pyx`.

**Exit criteria:** #399 reproduction passes; no Data-path regression.

### Phase 2 — Reader buffer export

1. Implement `_PrimitiveScalarListView` with full PEP 3118 metadata.
2. Route eligible `TYPE_LIST` returns through factory.
3. Lifetime tests mirroring `_BorrowedBufferView` / packed payload release.

**Exit criteria:** `memoryview(reader.samples)` zero-copy; indexing still works.

### Phase 3 — Polish (optional)

- Docs / `quickstart.rst` example
- Micro-benchmark in `benchmark/`
- `get_list_as_view(field)` explicit API if property typing surprises users

## Open Questions (Deferred)

- **List element `_set` fast path** — setting `list[i] = buffer` for primitive lists (not in v1).
- **Partial slice write** — `copy_(src, offset, size)` for list segments (separate from field-level assign).
- **Big-endian support** — byte-swap helper vs permanent slow path.
- **`List(Bool)`** — pack/unpack bitfield in C++ if ever needed.

## References

- [pycapnp#399 — buffer-protocol fast path for primitive List(T) fields](https://github.com/capnproto/pycapnp/issues/399)
- Existing buffer exporter: `capnp/lib/capnp.pyx` — `_BorrowedBufferView`
- Data field view design doc: `get-data-as-view-memory-leak.md`
