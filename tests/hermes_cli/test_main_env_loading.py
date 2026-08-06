def test_main_loads_user_and_project_environment(monkeypatch, tmp_path):
    import hermes_cli.main as main

    calls: list[dict[str, object]] = []

    def capture_load_hermes_dotenv(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(main, "load_hermes_dotenv", capture_load_hermes_dotenv)
    monkeypatch.setattr(main, "get_hermes_home", lambda: tmp_path / "hermes")

    main._load_entrypoint_environment()

    assert calls == [
        {
            "hermes_home": tmp_path / "hermes",
            "project_env": main.PROJECT_ROOT / ".env",
        }
    ]
