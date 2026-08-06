"""meteo_qa_cases.json 结构校验。

只校验 fixtures 结构（字段完整、expected_tools 三键、id 唯一、覆盖类别），
不调用真实工具，不依赖内网。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "meteo_qa_cases.json"
REQUIRED = [
    "id", "category", "question", "expected_tools", "key_facts",
    "time_scope", "spatial_scope", "units", "forbidden_phrases",
    "should_image", "should_gis",
]


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_cases_file_exists_and_parses():
    assert FIXTURE.exists(), f"缺少 {FIXTURE}"
    data = _load()
    assert isinstance(data, dict) and "cases" in data
    assert len(data["cases"]) >= 20, "至少覆盖 20 类问题"


def test_every_case_has_required_fields():
    data = _load()
    for case in data["cases"]:
        for field in REQUIRED:
            assert field in case, f"case {case.get('id')} 缺字段 {field}"
        assert set(case["expected_tools"].keys()) == {"allowed", "required", "forbidden"}, \
            f"case {case.get('id')} expected_tools 结构错误"


def test_case_ids_are_unique():
    data = _load()
    ids = [c["id"] for c in data["cases"]]
    assert len(ids) == len(set(ids)), f"case id 重复：{sorted(set(x for x in ids if ids.count(x) > 1))}"


def test_expected_tools_allowed_superset_of_required():
    """required 必须是 allowed 的子集（语义约束）。"""
    data = _load()
    for case in data["cases"]:
        allowed = set(case["expected_tools"]["allowed"])
        required = set(case["expected_tools"]["required"])
        assert required <= allowed, \
            f"case {case.get('id')} required 超出 allowed：{required - allowed}"
        forbidden = set(case["expected_tools"]["forbidden"])
        assert forbidden.isdisjoint(required), \
            f"case {case.get('id')} forbidden 与 required 冲突：{forbidden & required}"


def test_boolean_fields_are_bool():
    data = _load()
    for case in data["cases"]:
        assert isinstance(case["should_image"], bool), \
            f"case {case.get('id')} should_image 非 bool"
        assert isinstance(case["should_gis"], bool), \
            f"case {case.get('id')} should_gis 非 bool"
