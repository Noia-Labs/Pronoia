from app.config_compat import apply_legacy_names, default_llm_settings


def test_database_and_gateway_names_preserve_existing_data_location():
    env = {"FEVER_DB_PATH": "/existing/research.db", "FEVER_SIMULATION_GATEWAY_URL": "http://127.0.0.1:5010"}
    apply_legacy_names(env)
    assert env["PRONOIA_DB_PATH"] == "/existing/research.db"
    assert env["PRONOIA_SIMULATION_GATEWAY_URL"] == env["FEVER_SIMULATION_GATEWAY_URL"]
    env["PRONOIA_DB_PATH"] = "/explicit/new.db"
    apply_legacy_names(env)
    assert env["PRONOIA_DB_PATH"] == "/explicit/new.db"


def test_old_pronoia_provider_never_borrows_mirofish_key_or_model():
    env = {"ARK_API_URL": "https://old.example/v1", "ARK_API_KEY": "old-test-key", "ARK_MODEL": "old-model",
           "LLM_BASE_URL": "https://simulation.example/v1", "LLM_API_KEY": "simulation-test-key", "LLM_MODEL_NAME": "simulation-model"}
    assert default_llm_settings(env) == ("https://old.example/v1", "old-test-key", "old-model")
    env.update(LLM_API_URL="https://new.example/v1", LLM_API_KEY="new-test-key", LLM_MODEL="new-model")
    assert default_llm_settings(env) == ("https://new.example/v1", "new-test-key", "new-model")


def test_missing_legacy_key_is_not_filled_from_other_provider():
    assert default_llm_settings({"ARK_API_URL": "https://old.example/v1", "LLM_API_KEY": "unrelated-test-key"})[1] == ""
