"""Tests for the reliability/security-hardening configuration parsers
in app/config.py (LOG_LEVEL, MAX_REQUEST_BODY_BYTES, the three provider
timeouts, ANTHROPIC_MAX_RETRIES, and the three RATE_LIMIT_* vars).

Mirrors TestApiKeysConfigValidation in tests/test_auth.py: each parser
is exercised directly as a pure function, never by reimporting the app.
"""

import pytest

from app.config import (
    HardeningConfigError,
    _parse_bool,
    _parse_log_level,
    _parse_non_negative_int,
    _parse_positive_float,
    _parse_positive_int,
)


class TestParsePositiveInt:
    def test_valid_value(self):
        assert _parse_positive_int("X", "5") == 5

    def test_zero_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_positive_int("X", "0")

    def test_negative_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_positive_int("X", "-1")

    def test_non_numeric_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_positive_int("X", "abc")


class TestParseNonNegativeInt:
    def test_zero_is_allowed(self):
        assert _parse_non_negative_int("X", "0") == 0

    def test_positive_value(self):
        assert _parse_non_negative_int("X", "2") == 2

    def test_negative_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_non_negative_int("X", "-1")

    def test_non_numeric_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_non_negative_int("X", "abc")


class TestParsePositiveFloat:
    def test_valid_value(self):
        assert _parse_positive_float("X", "2.5") == 2.5

    def test_integer_string_is_accepted(self):
        assert _parse_positive_float("X", "5") == 5.0

    def test_zero_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_positive_float("X", "0")

    def test_negative_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_positive_float("X", "-1.0")

    def test_non_numeric_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_positive_float("X", "abc")


class TestParseBool:
    def test_true_variants(self):
        assert _parse_bool("X", "true") is True
        assert _parse_bool("X", "True") is True
        assert _parse_bool("X", " TRUE ") is True

    def test_false_variants(self):
        assert _parse_bool("X", "false") is False
        assert _parse_bool("X", "False") is False

    def test_invalid_value_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_bool("X", "yes")

    def test_empty_value_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_bool("X", "")


class TestParseLogLevel:
    def test_valid_levels_are_normalized_to_uppercase(self):
        assert _parse_log_level("info") == "INFO"
        assert _parse_log_level("Debug") == "DEBUG"
        assert _parse_log_level("ERROR") == "ERROR"

    def test_invalid_level_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_log_level("verbose")

    def test_empty_value_is_rejected(self):
        with pytest.raises(HardeningConfigError):
            _parse_log_level("")
