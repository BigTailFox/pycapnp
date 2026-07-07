# pycapnp `get_data_as_view()` Memory Retention Issue

`get_data_as_view()` appears to retain the backing reader/builder after the
memoryview is released

## Environment

- pycapnp: `2.2.3` (original report); re-verified on `2.2.4`
- Python: `3.11` (original report); re-verified on `3.10` (venv)
- OS: Ubuntu 22.04 x86_64 / WSL2

## Summary

Calling `get_data_as_view()` on a `Data` field seems to keep the backing
Cap'n Proto reader/builder alive even after:

- the returned `memoryview` is deleted
- `memoryview.release()` is called
- the reader/builder object is deleted
- `gc.collect()` and `malloc_trim(0)` are called

For packed readers, this also keeps the original packed input buffer alive. This
causes linear RSS growth when decoding messages with large `Data` fields in a
loop.

**Verification status: confirmed.** The reproducer below was re-run locally and
produced identical numbers to the original report.

**Fix status: implemented** in commit `ded8d02` (`fix: pin DATA field views via
shared buffer exporter`). See [Implementation](#implementation) and
[Testing](#testing).

## Expected Behavior

### Intended lifetime semantics

The desired contract for the returned `memoryview` (`mv`) is:

1. **While `mv` is alive** (Python refcount > 0): the underlying buffer must
   remain valid and must not be freed, even if the user deletes their Python
   variable for the reader/builder.
2. **Once `mv` is gone** (`del mv`, or refcount reaches 0 after
   `mv.release()`): the underlying buffer should become immediately collectible,
   even if reader/builder no longer exist in Python.

In other words, `mv` should pin the backing storage for the duration of its own
lifetime, and release that pin when `mv` itself is destroyed.

### Naive reference chain

A natural mental model is:

```text
mv ──ref──> reader/builder ──._parent──> _PackedMessageReaderBytes / _MessageBuilder ──> payload / allocator
```

For packed readers the full chain is:

```text
mv ──> _DynamicStructReader ──._parent──> _PackedMessageReaderBytes ──> input bytes + C++ PackedMessageReader
```

## Actual Behavior

The backing objects remain alive after `view.release()` and `del view`.

Observed with a 4 MiB `Data` field:

```text
reader_decode_only:                  +0.08 MiB/iter
reader_get_data_as_view_release:     +8.08 MiB/iter
builder_get_data_as_view_release:    +4.01 MiB/iter

view type: <class 'memoryview'> nbytes: 4194304 obj: None
payload alive after reader view release/del: True
```

Re-verification on pycapnp 2.2.4 (Python 3.10, WSL2):

```text
reader_decode_only:                  +0.08 MiB/iter
reader_get_data_as_view_release:     +8.08 MiB/iter
builder_get_data_as_view_release:    +4.01 MiB/iter

view type: <class 'memoryview'> nbytes: 4194304 obj: None
payload alive after reader view release/del: True
```

`from_bytes_packed()` alone does not leak. The leak appears only after calling
`reader.get_data_as_view("data")`.

## Minimal Reproducer

```python
from __future__ import annotations

import ctypes
import gc
import multiprocessing as mp
import tempfile
import weakref
from pathlib import Path

import capnp

SCHEMA_TEXT = """
@0x9d7d4f087df9b6e1;
struct BlobMsg {
  data @0 :Data;
}
"""

RAW = 4 * 1024 * 1024
ITERS = 20


class Payload(bytearray):
    pass


def trim() -> None:
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


def rss_mb() -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    raise RuntimeError("VmRSS not found")


def load_schema():
    td = tempfile.TemporaryDirectory()
    path = Path(td.name) / "blob.capnp"
    path.write_text(SCHEMA_TEXT)
    return td, capnp.load(str(path))


def make_packed(schema) -> bytes:
    msg = schema.BlobMsg.new_message()
    msg.data = b"\x55" * RAW
    return msg.to_bytes_packed()


def loop(case: str, q) -> None:
    td, schema = load_schema()
    try:
        trim()
        base = rss_mb()

        for _ in range(ITERS):
            if case == "reader_decode_only":
                packed = make_packed(schema)
                reader = schema.BlobMsg.from_bytes_packed(packed)
                _ = len(reader.data)
                del reader, packed

            elif case == "reader_get_data_as_view_release":
                packed = make_packed(schema)
                reader = schema.BlobMsg.from_bytes_packed(packed)
                view = reader.get_data_as_view("data")
                _ = len(view)
                view.release()
                del view, reader, packed

            elif case == "builder_get_data_as_view_release":
                builder = schema.BlobMsg.new_message()
                builder.init("data", RAW)
                view = builder.get_data_as_view("data")
                view[:] = b"\x55" * RAW
                view.release()
                del view, builder

            trim()

        end = rss_mb()
        q.put((case, base, end, end - base, (end - base) / ITERS))
    finally:
        td.cleanup()


def run(case: str):
    q = mp.Queue()
    p = mp.Process(target=loop, args=(case, q))
    p.start()
    p.join()
    if p.exitcode != 0:
        raise SystemExit(p.exitcode)
    return q.get()


def weakref_reader_case() -> bool:
    td, schema = load_schema()
    try:
        payload = Payload(make_packed(schema))
        ref = weakref.ref(payload)

        reader = schema.BlobMsg.from_bytes_packed(payload)
        view = reader.get_data_as_view("data")

        print("view type:", type(view), "nbytes:", view.nbytes, "obj:", view.obj)

        view.release()
        del view, reader, payload
        trim()

        return ref() is not None
    finally:
        td.cleanup()


print("pycapnp:", capnp.__version__)
print(f"raw_mib={RAW / 1024 / 1024:.1f}, iterations={ITERS}")

for case in [
    "reader_decode_only",
    "reader_get_data_as_view_release",
    "builder_get_data_as_view_release",
]:
    c, base, end, delta, per_iter = run(case)
    print(f"{c}: {base:.1f}->{end:.1f} MiB delta={delta:+.1f} per_iter={per_iter:+.2f}")

print("payload alive after reader view release/del:", weakref_reader_case())
```

## Analysis

### Buggy implementation (before fix)

In `capnp/lib/capnp.pyx`, both reader and builder `get_data_as_view()` follow
the same pattern:

```python
PyBuffer_FillInfo(&buf, self, data_ptr, data_size, readonly, flags)
return PyMemoryView_FromBuffer(&buf)   # success path: no PyBuffer_Release(&buf)
```

The comments in the source correctly note that `PyBuffer_FillInfo` takes a
reference to `self` via `buf.obj`, and that `PyBuffer_Release(&buf)` is called
only on the **exception** path.

### Root cause: leaked `Py_buffer` reference

Per the [Python C API](https://docs.python.org/3/c-api/memoryview.html#c.PyMemoryView_FromBuffer):

> `PyMemoryView_FromBuffer(view)` wraps the given buffer structure. The caller
> retains ownership of the buffer structure `view` and **must** call
> `PyBuffer_Release(view)` when `view` is no longer needed.

The call sequence inside `get_data_as_view()` is:

1. `PyBuffer_FillInfo(&buf, self, ptr, len, ...)` — `INCREF(self)` via `buf.obj`
2. `PyMemoryView_FromBuffer(&buf)` — copies the raw pointer into a new
   `memoryview` object
3. **Missing:** `PyBuffer_Release(&buf)` on the success path

Without step 3, each call permanently leaks one reference to the
reader/builder. After `del view, reader`, that leaked reference keeps the entire
chain alive (reader → `_PackedMessageReaderBytes` → packed input buffer).

This explains:

- `+8 MiB/iter` for packed readers (~4 MiB payload + ~4 MiB decoded message)
- `+4 MiB/iter` for builders (message segment only)
- `weakref payload` still alive after `view.release()` and `del view, reader`

### `PyMemoryView_FromBuffer` does NOT pin the exporter

CPython 3.11 source (`Objects/memoryobject.c`) makes an important distinction:

```c
/* info->obj is either NULL or a borrowed reference. This reference
   should not be decremented in PyBuffer_Release(). */
mbuf->master = *info;
mbuf->master.obj = NULL;
```

Key points:

- `PyMemoryView_FromBuffer()` copies the raw pointer but **forces
  `master.obj = NULL`**
- The returned `memoryview` therefore has **`mv.obj is None`** and does **not**
  hold a Python reference to the reader/builder
- `PyMemoryView_GET_BASE()` also returns `NULL` for views created this way

So the naive chain `mv ──ref──> reader` **does not exist** in the current
implementation. The `memoryview` is a bare pointer view, not an exporter-backed
view.

Additionally, the [buffer protocol docs](https://docs.python.org/3/c-api/buffer.html#c.PyBuffer_FillInfo) state that when `PyBuffer_FillInfo` is **not**
used inside a `getbufferproc`, the `exporter` argument **must be `NULL`**. The
current code passes `self` as exporter outside of `getbufferproc`, which is
outside the documented contract.

### Reference counts (buggy vs fixed)

Assume a single Python variable `reader` pointing at the struct reader.

| Stage | `mv` refcount | `reader` delta vs baseline | Notes |
|-------|---------------|----------------------------|-------|
| Before call | — | 0 | baseline |
| After `get_data_as_view()` (**buggy**) | **1** | **+1 (leaked)** | `PyMemoryView_FromBuffer` bare pointer; local `buf` never released |
| After `get_data_as_view()` (**fixed**) | **1** | **+1 (via exporter pin)** | `mv.obj` holds exporter; exporter._owner holds reader |
| After `del mv` (**buggy**) | 0 | **+1 (still leaked)** | leaked ref keeps reader/payload alive |
| After `del mv` (**fixed**) | 0 | **0** | reader collectable if no other refs |

Measured on pycapnp 2.2.4 (Python 3.10):

```text
# Buggy build
reader base=3, with mv=4 (+1), after del mv=4 (+1)
view.obj: None

# Fixed build (_BorrowedBufferView + PyMemoryView_FromObject)
reader base=3, with mv=4 (+1), after del mv=3 (+0)
view.obj: <capnp borrowed buffer view ...>
```

After the fix, the `+1` while `mv` is alive is intentional (exporter pins
reader). After `del mv`, the pin is released and memory becomes collectable.

### Lifetime semantics experiments

Additional tests were run to check the two intended lifetime rules.

#### Case 1: `del reader`, keep `mv`

```python
mv = reader.get_data_as_view("data")
del reader
len(mv)  # still works
```

| Build | `mv` usable after `del reader`? | payload alive while `mv` alive? |
|-------|--------------------------------|--------------------------------|
| Buggy | Yes | Yes |
| Fixed | Yes | Yes |

After the fix, this works by design: `mv → exporter → reader` keeps the reader
object alive even when the user's `reader` variable is deleted.

#### Case 2: `del reader`, then `del mv` — should release buf

| Build | payload collected? |
|-------|--------------------|
| Buggy | **No** |
| Fixed | **Yes** |

#### Case 3: loop 20× (`del reader` then `del mv` each iteration)

| Build | RSS per iteration |
|-------|-------------------|
| Buggy | +8.00 MiB/iter |
| Fixed | +0.00 MiB/iter |

## Implementation

Commit: `fix: pin DATA field views via shared buffer exporter`

The fix generalizes the existing `_SegmentView` infrastructure so that
`get_data_as_view()` and `to_segment_views()` share one buffer-protocol exporter
class. The old `PyBuffer_FillInfo` + `PyMemoryView_FromBuffer` path is removed
entirely from `get_data_as_view()`.

### Design

The `_SegmentView` helper in `capnp/lib/capnp.pyx` already implemented the
correct buffer-protocol pattern:

```text
consumer ──> memoryview ──ref──> exporter ──._owner──> message / struct ──> backing storage
```

Both APIs now share infrastructure but keep different public return shapes:

| API | Owner pinned | Read-only | Public return type |
|-----|--------------|-----------|-------------------|
| `to_segment_views()` | `_MessageBuilder` | always | sequence of exporters |
| `get_data_as_view()` (reader) | `_DynamicStructReader` | yes | `memoryview` |
| `get_data_as_view()` (builder) | `_DynamicStructBuilder` | no | `memoryview` |

### `_BorrowedBufferView`

`_SegmentView` was generalized to `_BorrowedBufferView`: a generic exporter
with `_owner`, `_ptr`, `_size`, and `_readonly` fields, implementing
`__getbuffer__` / `__releasebuffer__`.

`_SegmentViews` now creates `_BorrowedBufferView(..., readonly=True)` instances
instead of `_SegmentView`. No public API change for `to_segment_views()`.

### `_memoryview_borrowing` helper

```cython
from cpython.memoryview cimport PyMemoryView_FromObject

cdef inline object _memoryview_borrowing(object owner, void* ptr,
                                         Py_ssize_t size, bint readonly):
    cdef _BorrowedBufferView exporter
    exporter = _BorrowedBufferView()._init(owner, ptr, size, readonly)
    return PyMemoryView_FromObject(exporter)
```

This establishes the reference chain:

```text
memoryview ──ref──> _BorrowedBufferView ──._owner──> reader/builder ──._parent──> message / payload
```

`mv.obj` now points at the exporter object (not `None`), and lifetime is
enforced by standard buffer-protocol reference counting.

### `get_data_as_view()` on reader and builder

DATA field pointer resolution is factored into `_data_field_ptr_reader` and
`_data_field_ptr_builder`. Both `get_data_as_view()` methods resolve the field
pointer then call `_memoryview_borrowing`:

```cython
# reader (read-only)
_data_field_ptr_reader(self, field, &data_ptr, &data_size)
return _memoryview_borrowing(self, data_ptr, <Py_ssize_t>data_size, True)

# builder (writable)
_data_field_ptr_builder(self, field, &data_ptr, &data_size)
return _memoryview_borrowing(self, data_ptr, <Py_ssize_t>data_size, False)
```

Empty-field handling is unchanged (`data_size == 0 and data_ptr == NULL` →
`_EMPTY_DATA_VIEW_SENTINEL`).

### Owner pinning: struct vs message

- **`to_segment_views()`** exports whole-message segment snapshots → pins
  `_MessageBuilder`.
- **`get_data_as_view()`** exports a pointer inside a specific struct's DATA
  field → pins **`self`** (the struct reader/builder):

  ```text
  # packed reader
  mv → exporter → _DynamicStructReader → _PackedMessageReaderBytes → payload

  # builder
  mv → exporter → _DynamicStructBuilder → _MessageBuilder → arena
  ```

### API compatibility

- **`get_data_as_view()`** still returns `memoryview` directly — no breaking
  change for callers.
- **`to_segment_views()`** still returns a sequence of exporter objects;
  callers wrap with `memoryview()` as before.

### Files changed

| File | Changes |
|------|---------|
| `capnp/lib/capnp.pyx` | `_BorrowedBufferView`, `_memoryview_borrowing`, `_data_field_ptr_*`, refactor `_SegmentViews`, rewrite both `get_data_as_view()` |
| `test/test_get_data_view.py` | Add lifetime / leak regression tests |

### Structure

```text
_SegmentView  ──generalize──>  _BorrowedBufferView (+ _readonly, _owner)
         │                              ▲
         │                              │
_SegmentViews ──────────────────────────┘  (readonly=True, owner=builder)

get_data_as_view (reader)  ──> _memoryview_borrowing(self, ptr, len, readonly=True)
get_data_as_view (builder) ──> _memoryview_borrowing(self, ptr, len, readonly=False)
```

## Testing

### New regression tests (`test/test_get_data_view.py`)

| Test | What it verifies |
|------|-----------------|
| `test_data_view_exports_through_buffer_exporter` | `view.obj is not None`; exporter length matches view |
| `test_data_view_survives_del_builder` | `del msg` while view alive → view still readable |
| `test_data_view_releases_packed_payload` | after `view.release()` + `del view, reader, payload` → weakref payload is `None` |

Existing tests continue to cover read-only/writable behavior, empty DATA
fields, nested structs, wrong field types, and `test_view_keeps_message_alive`
(refcount increase via exporter pin).

### Targeted test runs

```bash
python -m pytest test/test_get_data_view.py test/test_serialization.py -q
# 36 passed
```

All segment-view tests pass unchanged, confirming `_BorrowedBufferView`
generalization did not regress `to_segment_views()` behavior.

### Full test suite

```bash
python -m pytest test/ -q
```

Run on pycapnp 2.2.4, Python 3.10, WSL2 (with async test deps installed):

| Result | Count |
|--------|-------|
| Passed | 151 |
| Failed | 1 |
| XFailed | 1 |
| Total | 153 |

The single failure is unrelated to this fix:

- `test/test_load.py::test_bundled_import_hook` — `ImportError: cannot import
  name 'stream_capnp' from 'capnp'`. This is a dev-environment issue: bundled
  `.capnp` schemas are not on the import path under editable installs. CI/tox
  runs `pip install .` first and does not hit this.

### Reproducer after fix

Re-running the RSS reproducer from this document on the fixed build:

```text
reader_decode_only:                  +0.08 MiB/iter
reader_get_data_as_view_release:     +0.08 MiB/iter
builder_get_data_as_view_release:    +0.00 MiB/iter

view type: <class 'memoryview'> nbytes: 4194304 obj: <capnp borrowed buffer view ...>
payload alive after reader view release/del: False
```

## Notes

In the buggy build, the returned `memoryview` reported `obj is None`, and
explicit `memoryview.release()` did not release the backing owner. The apparent
"pinning while `mv` is alive" was largely a side effect of the leaked
`PyBuffer_FillInfo` reference, not a correct exporter-backed buffer view.

After the fix, `mv.obj` exposes the internal `_BorrowedBufferView` exporter,
and lifetime follows standard Python buffer-protocol semantics: the view pins
the struct reader/builder (and thus the message/payload) while alive, and
releases it when the view is destroyed.
