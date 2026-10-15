import json
from app.providers.nvidia_nim import normalize_completion_content, unwrap_item_arrays
from app.planner import PlanningReply


def test_unwrap_item_arrays():
    data = {
        "reply": "hello",
        "questions": [],
        "read_paths": {"item": ["path1", "path2"]},
        "plan": {
            "goal": "test goal",
            "acceptance_criteria": {"item": ["crit 1"]},
            "files_to_inspect": {"item": ["f1"]},
            "files_likely_to_change": {"item": ["f2"]},
            "implementation_steps": {"item": ["step 1"]},
            "tests_to_add_or_run": {"item": ["test 1"]},
            "commands_to_run": {
                "item": [
                    {
                        "command_id": "c1",
                        "purpose": "run tests",
                        "argv": {"item": ["pytest", "-q"]},
                        "working_directory": ".",
                    }
                ]
            },
            "risks": {"item": ["risk 1"]},
            "questions_for_maintainer": {"item": ["q1"]},
        },
    }
    unwrapped = unwrap_item_arrays(data)
    assert unwrapped["read_paths"] == ["path1", "path2"]
    assert unwrapped["plan"]["acceptance_criteria"] == ["crit 1"]
    assert unwrapped["plan"]["commands_to_run"][0]["argv"] == ["pytest", "-q"]


def test_normalize_completion_content_with_markdown():
    raw = """```json
{"reply": "test", "questions": [], "read_paths": {"item": ["a", "b"]}, "plan": null}
```"""
    normalized = normalize_completion_content(raw)
    parsed = json.loads(normalized)
    assert parsed["reply"] == "test"
    assert parsed["read_paths"] == ["a", "b"]
    reply = PlanningReply.model_validate_json(normalized)
    assert reply.read_paths == ["a", "b"]


def test_normalize_completion_content_planning_reply():
    raw = json.dumps(
        {
            "reply": "完整改造方案",
            "questions": [],
            "read_paths": [],
            "plan": {
                "goal": "Migrate Puppeteer tests to Playwright",
                "acceptance_criteria": {"item": ["Delete puppeteer specs", "Playwright passes"]},
                "files_to_inspect": {"item": ["core/tests/puppeteer.ts"]},
                "files_likely_to_change": {"item": ["core/tests/playwright.ts"]},
                "implementation_steps": {"item": ["Step 1", "Step 2"]},
                "tests_to_add_or_run": {"item": ["Run playwright test"]},
                "commands_to_run": {
                    "item": [
                        {
                            "command_id": "c1",
                            "purpose": "Run acceptance test",
                            "argv": {"item": ["npx", "playwright", "test"]},
                            "working_directory": ".",
                        }
                    ]
                },
                "risks": {"item": ["Flaky test"]},
                "questions_for_maintainer": {"item": ["Question for maintainer"]},
            },
        }
    )
    normalized = normalize_completion_content(raw)
    reply = PlanningReply.model_validate_json(normalized)
    assert reply.plan is not None
    assert reply.plan.acceptance_criteria == ["Delete puppeteer specs", "Playwright passes"]
    assert reply.plan.commands_to_run[0].argv == ["npx", "playwright", "test"]


def test_normalize_empty_list_strings_and_nulls():
    raw = json.dumps(
        {
            "reply": "方案说明",
            "questions": "",
            "read_paths": "",
            "plan": {
                "goal": "Migrate Puppeteer tests to Playwright",
                "acceptance_criteria": ["Acceptance 1"],
                "files_to_inspect": ["f1.py"],
                "files_likely_to_change": ["f1.py"],
                "implementation_steps": ["Step 1"],
                "tests_to_add_or_run": ["Test 1"],
                "commands_to_run": [
                    {
                        "command_id": "c1",
                        "purpose": "test",
                        "argv": "pytest -q",
                        "working_directory": ".",
                    }
                ],
                "risks": "",
                "questions_for_maintainer": "",
            },
        }
    )
    normalized = normalize_completion_content(raw)
    reply = PlanningReply.model_validate_json(normalized)
    assert reply.plan is not None
    assert reply.plan.questions_for_maintainer == []
    assert reply.plan.risks == []
    assert reply.plan.commands_to_run[0].argv == ["pytest", "-q"]
    assert reply.questions == []
    assert reply.read_paths == []


def test_normalize_empty_plan_with_questions():
    raw = json.dumps(
        {
            "reply": "我有疑问",
            "questions": [
                {
                    "id": "q1",
                    "prompt": "Which framework?",
                    "options": "",
                }
            ],
            "read_paths": "",
            "plan": "",
        }
    )
    normalized = normalize_completion_content(raw)
    reply = PlanningReply.model_validate_json(normalized)
    assert reply.plan is None
    assert len(reply.questions) == 1
    assert reply.questions[0].options == []


def test_normalize_unescaped_quotes():
    broken = '{"reply": "请在 PR 前跑 "python -m scripts.linters" 检查。", "questions": [], "read_paths": ["core/file.ts"], "plan": null}'
    normalized = normalize_completion_content(broken)
    reply = PlanningReply.model_validate_json(normalized)
    assert 'python -m scripts.linters' in reply.reply
    assert reply.read_paths == ["core/file.ts"]
    assert reply.plan is None
