"""QQ 表情 id↔名称表：条数、唯一性、双向反查。"""

from __future__ import annotations

from dafeiyu.faces import FACE_NAMES, face_id, face_name


def test_face_names_has_109_unique_entries() -> None:
    assert len(FACE_NAMES) == 109
    assert len(set(FACE_NAMES)) == 109
    assert all(isinstance(sid, int) for sid in FACE_NAMES)


def test_face_names_are_non_empty_and_unique() -> None:
    names = list(FACE_NAMES.values())
    assert all(isinstance(name, str) and name.strip() for name in names)
    assert len(set(names)) == len(names)


def test_face_name_accepts_int_and_numeric_string() -> None:
    assert face_name(14) == "微笑"
    assert face_name("1") == "撇嘴"
    assert face_name(9999) is None
    assert face_name("abc") is None


def test_face_name_rejects_bool() -> None:
    # bool 是 int 子类，但不能把 True 静默当成 1。
    assert face_name(True) is None
    assert face_name(False) is None


def test_face_id_lookup_is_case_insensitive_for_ascii() -> None:
    assert face_id("微笑") == "14"
    assert face_id("doge") == "179"
    assert face_id("DOGE") == "179"
    assert face_id("不存在") is None
