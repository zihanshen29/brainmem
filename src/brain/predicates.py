"""Small explicit vocabulary; unknown predicates never imply single cardinality."""

import re

# canonical: (cardinality, aliases, human-readable Chinese label)
VOCABULARY = {
    "current_commit": ("one", ("has_current_commit", "has_production_commit"), "当前提交"),
    "database_head": ("one", ("db_head", "has_database_head", "has_alembic_head"), "数据库版本"),
    "status": ("many", ("has_status", "current_status"), "进展"),
    "lifecycle_state": ("one", (), "生命周期状态"),
    "works_at": ("one", ("employed_by",), "任职于"),
    "role": ("one", ("has_role",), "角色"),
    "lives_in": ("one", ("resides_in",), "居住地"),
    "location": ("one", ("located_in", "current_location"), "所在地"),
    "works_as": ("one", (), "工作角色"),
    "uses": ("many", ("uses_tool", "use"), "使用"),
    "committed": ("many", ("has_commit",), "完成提交"),
    "passed": ("many", ("passed_test", "passes"), "通过"),
    "decided": ("many", ("decision", "has_decision"), "决定"),
    "prefers": ("many", ("preference",), "偏好"),
    "related_to": ("many", (), "关联"),
}


def normalize_predicate(value: str) -> str:
    key = re.sub(r"[\s-]+", "_", value.strip().casefold())
    for canonical, (_, aliases, _) in VOCABULARY.items():
        if key == canonical or key in aliases:
            return canonical
    return key


def is_single_valued(value: str) -> bool:
    entry = VOCABULARY.get(normalize_predicate(value))
    return entry is not None and entry[0] == "one"


def fact_sentence(subject: str, predicate: str, value: str, *, chinese: bool = False) -> str:
    key = normalize_predicate(predicate)
    if chinese:
        label = VOCABULARY.get(key, (None, (), key))[2]
        return f"{subject}: {label} {value}。"
    return f"{subject}: {key.replace('_', ' ')} {value}."


def uses_chinese(text: str, output_language: str = "source") -> bool:
    """Honor explicit Chinese/English rendering, otherwise follow the evidence."""
    language = output_language.strip().casefold()
    if language == "zh" or language.startswith("zh-"):
        return True
    if language == "en" or language.startswith("en-"):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", text))
