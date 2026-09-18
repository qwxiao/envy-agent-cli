"""输出契约层测试：只做结构校验，不碰业务语义。"""

from envy_agent_cli.llm.validator import OutputContract, build_correction_prompt, validate_output

ANSWER = OutputContract(
    name="answer",
    schema={
        "type": "object",
        "required": ["summary", "confidence"],
        "properties": {
            "summary": {"type": "string"},
            "confidence": {"type": "number"},
            "level": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    },
)


def test_valid_output_passes_and_is_parsed():
    outcome = validate_output('{"summary": "ok", "confidence": 0.9}', ANSWER)
    assert outcome.ok is True
    assert outcome.value == {"summary": "ok", "confidence": 0.9}
    assert outcome.correction_prompt == ""


def test_empty_output_fails():
    assert validate_output("   ", ANSWER).ok is False


def test_non_json_output_fails():
    outcome = validate_output("答案是 42。", ANSWER)
    assert outcome.ok is False
    assert "合法 JSON" in outcome.error


def test_non_object_toplevel_fails():
    outcome = validate_output("[1, 2, 3]", ANSWER)
    assert outcome.ok is False and "顶层应为对象" in outcome.error


def test_missing_required_field_fails():
    outcome = validate_output('{"summary": "ok"}', ANSWER)
    assert outcome.ok is False and "confidence" in outcome.error


def test_wrong_type_fails():
    outcome = validate_output('{"summary": 1, "confidence": 0.9}', ANSWER)
    assert outcome.ok is False and "类型应为 string" in outcome.error


def test_enum_violation_fails():
    outcome = validate_output('{"summary": "ok", "confidence": 0.9, "level": "urgent"}', ANSWER)
    assert outcome.ok is False and "enum" not in outcome.error and "level" in outcome.error


def test_business_rules_are_not_checked_here():
    """路径白名单、跨字段业务语义这类"需要懂业务"的校验归上层，M1 不管。"""
    contract = OutputContract("fs", {"type": "object", "required": ["path"],
                                     "properties": {"path": {"type": "string"}}})
    assert validate_output('{"path": "../../.env"}', contract).ok is True


def test_correction_prompt_is_model_facing_not_a_traceback():
    outcome = validate_output('{"summary": "ok"}', ANSWER)
    prompt = outcome.correction_prompt
    assert "answer" in prompt and "confidence" in prompt
    assert "Traceback" not in prompt and "File \"" not in prompt


def test_build_correction_prompt_includes_schema():
    prompt = build_correction_prompt(ANSWER, "缺少必填字段：confidence")
    assert "confidence" in prompt and "JSON" in prompt
