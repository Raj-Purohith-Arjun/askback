"""Core: registry, config expansion and the shared text utilities."""

from __future__ import annotations

import pytest

from askback.core.config import AppConfig, _detect_project_root, default_samples, load_mapping
from askback.core.errors import ConfigError, RegistryError
from askback.core.registry import Registry
from askback.core.text import (
    coverage,
    fold_plural,
    jaccard,
    normalize,
    sentences,
    tokenize,
)


class TestRegistry:
    def test_register_and_get(self) -> None:
        registry: Registry = Registry("widget")

        @registry.register("alpha")
        class Alpha:
            pass

        assert registry.get("alpha") is Alpha
        assert registry.get("ALPHA") is Alpha, "lookup must be case-insensitive"
        assert "alpha" in registry
        assert registry.names() == ["alpha"]

    def test_duplicate_name_is_rejected(self) -> None:
        registry: Registry = Registry("widget")
        registry.register("alpha", object())
        with pytest.raises(RegistryError, match="already registered"):
            registry.register("alpha", object())

    def test_unknown_name_lists_options(self) -> None:
        registry: Registry = Registry("widget")
        registry.register("alpha", object())
        with pytest.raises(RegistryError, match="alpha"):
            registry.get("missing")


class TestDefaultSamples:
    def test_absent_means_one_draw(self) -> None:
        assert default_samples({}) == 1
        assert default_samples({"default_samples": 3}) == 3

    @pytest.mark.parametrize("value", [0, -1, "3", 2.5, True, None])
    def test_anything_but_a_positive_whole_number_is_refused(self, value: object) -> None:
        with pytest.raises(ConfigError, match="default_samples"):
            default_samples({"default_samples": value})


class TestConfig:
    def test_env_expansion_with_default(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "app.yaml"
        path.write_text(
            "server:\n"
            "  host: ${TEST_HOST:127.0.0.1}\n"
            "  port: ${TEST_PORT:8000}\n"
            "  debug: ${TEST_DEBUG:false}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("TEST_PORT", "9999")

        data = load_mapping(path)
        assert data["server"]["host"] == "127.0.0.1", "unset var falls back to the default"
        assert data["server"]["port"] == 9999, "numeric env vars are coerced to int"
        assert data["server"]["debug"] is False, "boolean defaults are coerced too"

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_mapping(tmp_path / "nope.yaml")

    def test_a_path_that_does_not_exist_is_an_error_not_a_default(self, tmp_path) -> None:
        # It used to return an empty config, which turned a typo in --config into a run of
        # something else.
        with pytest.raises(ConfigError, match="Config file not found"):
            AppConfig.load(tmp_path / "absent.yaml")

    def test_no_path_at_all_is_the_builtin_demo(self, monkeypatch) -> None:
        monkeypatch.delenv("ASKBACK_CONFIG", raising=False)
        config = AppConfig.load()
        assert AppConfig.is_builtin(None) and config.raw["agent"]["adapter"] == "demo"
        # No suite floors and no suite names: nothing here can be tripped by deleting a suite.
        assert "suite_must_pass" not in config.evaluation and "default_suites" not in config.evaluation

    def test_the_config_env_var_replaces_the_demo(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "mine.yaml"
        path.write_text("evaluation:\n  dataset_dir: suites\n", encoding="utf-8")
        monkeypatch.setenv("ASKBACK_CONFIG", str(path))
        assert not AppConfig.is_builtin(None)
        assert AppConfig.load().evaluation["dataset_dir"] == "suites"

    def test_home_env_var_overrides_root_detection(self, tmp_path, monkeypatch) -> None:
        # Set by the container image, because an installed package cannot infer
        # where config/ and data/ live.
        monkeypatch.setenv("ASKBACK_HOME", str(tmp_path))
        assert _detect_project_root() == tmp_path.resolve()

    def test_source_checkout_is_detected_without_env(self, monkeypatch) -> None:
        monkeypatch.delenv("ASKBACK_HOME", raising=False)
        root = _detect_project_root()
        assert (root / "pyproject.toml").is_file()

    def test_unknown_sections_survive_in_raw(self, tmp_path) -> None:
        path = tmp_path / "app.yaml"
        path.write_text("custom:\n  key: value\nllm:\n  provider: extractive\n", encoding="utf-8")
        config = AppConfig.load(path)
        assert config.section("custom") == {"key": "value"}


class TestText:
    def test_tokenize_drops_stopwords_and_folds_plurals(self) -> None:
        assert tokenize("How many expense claims are there?") == ["many", "expense", "claim"]

    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("days", "day"),
            ("policies", "policy"),
            ("business", "business"),
            ("status", "status"),
            ("analysis", "analysis"),
            ("is", "is"),
        ],
    )
    def test_fold_plural_is_conservative(self, word: str, expected: str) -> None:
        assert fold_plural(word) == expected

    def test_sentences_split_on_terminators(self) -> None:
        assert sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]

    def test_normalize_collapses_whitespace_and_case(self) -> None:
        assert normalize("  Two   Words\n") == "two words"

    def test_coverage_is_asymmetric(self) -> None:
        # Every claim token is in the source, even though the source has extras.
        assert coverage(["a", "b"], ["a", "b", "c"]) == 1.0
        assert coverage(["a", "b", "c"], ["a"]) == pytest.approx(1 / 3)

    def test_jaccard_handles_empty_input(self) -> None:
        assert jaccard([], ["a"]) == 0.0
