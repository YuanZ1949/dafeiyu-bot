"""QQ 内置表情（sysface）id ↔ 名称表。

来源：NapCat ``packages/napcat-core/external/face_config.json`` 的 ``sysface``，
取 ``0 <= QSid <= 183``，``QDes`` 去掉前导 ``/``，逐字保留 109 条。
入站 ``face`` 段带数字 id，出站模型可用名称或 id；两边都靠这张表对齐。
"""

from __future__ import annotations

FACE_NAMES: dict[int, str] = {
    0: "惊讶", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪",
    6: "害羞", 7: "闭嘴", 8: "睡", 9: "大哭", 10: "尴尬", 11: "发怒",
    12: "调皮", 13: "呲牙", 14: "微笑", 15: "难过", 16: "酷", 18: "抓狂",
    19: "吐", 20: "偷笑", 21: "可爱", 22: "白眼", 23: "傲慢", 24: "饥饿",
    25: "困", 26: "惊恐", 27: "流汗", 28: "憨笑", 29: "悠闲", 30: "奋斗",
    31: "咒骂", 32: "疑问", 33: "嘘", 34: "晕", 35: "折磨", 36: "衰",
    37: "骷髅", 38: "敲打", 39: "再见", 41: "发抖", 42: "爱情", 43: "跳跳",
    46: "猪头", 49: "拥抱", 53: "蛋糕", 55: "炸弹", 56: "刀", 59: "便便",
    60: "咖啡", 63: "玫瑰", 64: "凋谢", 66: "爱心", 67: "心碎", 74: "太阳",
    75: "月亮", 76: "赞", 77: "踩", 78: "握手", 79: "胜利", 85: "飞吻",
    86: "怄火", 89: "西瓜", 96: "冷汗", 97: "擦汗", 98: "抠鼻", 99: "鼓掌",
    100: "糗大了", 101: "坏笑", 102: "左哼哼", 103: "右哼哼", 104: "哈欠", 105: "鄙视",
    106: "委屈", 107: "快哭了", 108: "阴险", 109: "左亲亲", 110: "吓", 111: "可怜",
    112: "菜刀", 114: "篮球", 116: "示爱", 118: "抱拳", 119: "勾引", 120: "拳头",
    121: "差劲", 122: "爱你", 123: "NO", 124: "OK", 125: "转圈", 129: "挥手",
    137: "鞭炮", 144: "喝彩", 146: "爆筋", 147: "棒棒糖", 148: "喝奶", 169: "手枪",
    171: "茶", 172: "眨眼睛", 173: "泪奔", 174: "无奈", 175: "卖萌", 176: "小纠结",
    177: "喷血", 178: "斜眼笑", 179: "doge", 180: "惊喜", 181: "戳一戳", 182: "笑哭",
    183: "我最美",
}

#: 名称 → id 的反查表；ASCII 名称大小写不敏感（``DOGE`` == ``doge``）。
_FACE_IDS: dict[str, int] = {
    name.casefold(): sid for sid, name in FACE_NAMES.items()
}


def face_name(face_id: object) -> str | None:
    """表情 id → 名称；非数字或表外返回 None。

    ``int`` 与纯数字 ``str`` 都接受；``bool`` 不算数字（避免 True 被当作 1）。
    """
    if isinstance(face_id, bool):
        return None
    if isinstance(face_id, int):
        return FACE_NAMES.get(face_id)
    if isinstance(face_id, str):
        value = face_id.strip()
        if value.isdigit():
            return FACE_NAMES.get(int(value))
    return None


def face_id(name: object) -> str | None:
    """表情名称 → id 字符串；``str()`` 后 strip，ASCII 名称大小写不敏感。

    未命中返回 None；本表 109 条名称无重复，因此命中即唯一。
    """
    value = str(name).strip()
    if not value:
        return None
    matched = _FACE_IDS.get(value.casefold())
    return str(matched) if matched is not None else None
