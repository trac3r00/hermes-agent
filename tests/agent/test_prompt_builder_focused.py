import pytest

from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache


@pytest.fixture(autouse=True)
def clear_skills_system_prompt_cache_fixture():
    clear_skills_system_prompt_cache(clear_snapshot=True)
    yield
    clear_skills_system_prompt_cache(clear_snapshot=True)


def write_skill(tmp_path, category, name, description, metadata=""):
    skill_dir = tmp_path / "skills" / category / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{metadata}---\n"
    )


def test_full_mode_keeps_non_keep_category_descriptions(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_skill(tmp_path, "research", "deep-research", "Research unfamiliar topics")

    result = build_skills_system_prompt(index_mode="full")

    assert "deep-research" in result
    assert "Research unfamiliar topics" in result


def test_focused_mode_demotes_non_keep_categories_and_keeps_names(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_skill(tmp_path, "coding", "python-debug", "Debug Python programs")
    write_skill(tmp_path, "research", "deep-research", "Research unfamiliar topics")

    result = build_skills_system_prompt(index_mode="focused")

    assert "python-debug" in result
    assert "Debug Python programs" in result
    assert "research [names only]: deep-research" in result
    assert "Research unfamiliar topics" not in result
    assert "descriptions omitted to save space" in result
    assert "skill_view(name)" in result


def test_focused_mode_is_materially_smaller(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    long_description = "A detailed procedure for this specialized workflow. " * 10
    for category, name in (
        ("research", "deep-research"),
        ("media", "video-editing"),
        ("social", "community-management"),
    ):
        write_skill(tmp_path, category, name, long_description)
    write_skill(tmp_path, "coding", "python-debug", long_description)

    full = build_skills_system_prompt(index_mode="full")
    focused = build_skills_system_prompt(index_mode="focused")

    assert len(focused) < len(full)


def test_cache_key_isolates_full_and_focused_modes(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_skill(tmp_path, "research", "deep-research", "Research unfamiliar topics")

    full = build_skills_system_prompt(index_mode="full")
    focused = build_skills_system_prompt(index_mode="focused")

    assert full != focused
    assert "Research unfamiliar topics" in full
    assert "Research unfamiliar topics" not in focused


def test_explicit_index_mode_overrides_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"skills": {"index_mode": "focused"}},
    )
    write_skill(tmp_path, "research", "deep-research", "Research unfamiliar topics")

    configured = build_skills_system_prompt()
    explicit = build_skills_system_prompt(index_mode="full")

    assert "Research unfamiliar topics" not in configured
    assert "Research unfamiliar topics" in explicit


def test_required_deferred_tool_skill_stays_listed_when_available(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_skill(
        tmp_path,
        "research",
        "specialist-research",
        "Use the specialist research tool",
        "metadata:\n  hermes:\n    requires_tools: [some_specialist_tool]\n",
    )

    result = build_skills_system_prompt(
        available_tools={"some_specialist_tool"},
        available_toolsets=set(),
        index_mode="focused",
    )

    assert "specialist-research" in result
