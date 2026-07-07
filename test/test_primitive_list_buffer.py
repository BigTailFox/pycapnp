import array
import gc
import os
import weakref

import pytest
import capnp


@pytest.fixture(scope="module")
def all_types():
    return capnp.load(os.path.join(os.path.dirname(__file__), "all_types.capnp"))


def test_dynamic_int16_list_accepts_memoryview(all_types):
    msg = all_types.TestAllTypes.new_message()
    values = array.array("h", [1234, -5678, -32768, 32767])

    msg.int16List = memoryview(values)

    assert list(msg.int16List) == list(values)
    assert bytes(memoryview(msg.int16List)) == values.tobytes()


def test_dynamic_float32_list_accepts_array(all_types):
    msg = all_types.TestAllTypes.new_message()
    values = array.array("f", [0.0, 1.25, -2.5, 8.0])

    msg.float32List = values

    view = memoryview(msg.float32List)
    assert view.itemsize == values.itemsize
    assert view.nbytes == len(values) * values.itemsize
    assert bytes(view) == values.tobytes()
    assert msg.float32List[1] == pytest.approx(1.25)


def test_reader_numeric_list_exports_readonly_buffer(all_types):
    builder = all_types.TestAllTypes.new_message()
    values = array.array("I", [0, 1, 2, 0xFFFFFFFF])
    builder.uInt32List = values
    payload = builder.to_bytes()

    with all_types.TestAllTypes.from_bytes(payload) as reader:
        lst = reader.uInt32List
        view = memoryview(lst)

        assert len(lst) == len(values)
        assert lst[3] == 0xFFFFFFFF
        assert view.readonly is True
        assert view.itemsize == values.itemsize
        assert view.nbytes == len(values) * values.itemsize
        assert bytes(view) == values.tobytes()


def test_builder_numeric_list_exports_writable_buffer(all_types):
    msg = all_types.TestAllTypes.new_message()
    msg.uInt8List = b"\x01\x02\x03"

    view = memoryview(msg.uInt8List)

    assert view.readonly is False
    assert bytes(view) == b"\x01\x02\x03"
    view.cast("B")[1] = 9
    assert list(msg.uInt8List) == [1, 9, 3]


def test_builder_numeric_list_supports_index_assignment(all_types):
    msg = all_types.TestAllTypes.new_message()
    msg.int16List = array.array("h", [1, 2, 3])

    msg.int16List[1] = -42

    assert list(msg.int16List) == [1, -42, 3]


def test_mistyped_numeric_buffer_falls_through(all_types):
    msg = all_types.TestAllTypes.new_message()

    with pytest.raises(capnp.KjException, match="unsupported type"):
        msg.int16List = array.array("H", [1, 2, 3])


def test_numeric_list_to_dict_still_materializes_list(all_types):
    msg = all_types.TestAllTypes.new_message()
    msg.int16List = array.array("h", [1, 2, 3])

    assert msg.to_dict()["int16List"] == [1, 2, 3]


def test_non_numeric_lists_do_not_export_buffer(all_types):
    msg = all_types.TestAllTypes.new_message()
    msg.boolList = [True, False]
    msg.textList = ["a", "b"]

    with pytest.raises(TypeError):
        memoryview(msg.boolList)
    with pytest.raises(TypeError):
        memoryview(msg.textList)


def test_empty_numeric_list_exports_zero_length_buffer(all_types):
    msg = all_types.TestAllTypes.new_message()
    msg.init("int16List", 0)

    lst = msg.int16List
    view = memoryview(lst)

    assert len(lst) == 0
    assert view.nbytes == 0
    assert bytes(view) == b""


def test_numeric_list_view_exports_through_buffer_exporter(all_types):
    msg = all_types.TestAllTypes.new_message()
    msg.int16List = array.array("h", [1, 2, 3])

    view = memoryview(msg.int16List)

    assert isinstance(view, memoryview)
    assert view.obj is not None
    assert isinstance(view.obj, capnp._PrimitiveScalarListView)


def test_numeric_list_view_survives_del_builder(all_types):
    msg = all_types.TestAllTypes.new_message()
    msg.int16List = array.array("h", [10, 20, 30])
    view = memoryview(msg.int16List)

    del msg
    gc.collect()

    assert bytes(view) == array.array("h", [10, 20, 30]).tobytes()


def test_numeric_list_view_releases_after_memoryview_release(all_types):
    msg = all_types.TestAllTypes.new_message()
    msg.int16List = array.array("h", [1, 2, 3])
    lst = msg.int16List
    lst_ref = weakref.ref(lst)

    view = memoryview(lst)
    view.release()

    del view, lst, msg
    gc.collect()

    assert lst_ref() is None


def test_assign_primitive_list_view_copies_field(all_types):
    src = all_types.TestAllTypes.new_message()
    dst = all_types.TestAllTypes.new_message()
    src.int16List = array.array("h", [7, 8, 9])

    dst.int16List = src.int16List

    assert list(dst.int16List) == [7, 8, 9]
    src.int16List[0] = 0
    assert list(dst.int16List) == [7, 8, 9]
