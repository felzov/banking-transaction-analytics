"""The app's own log lines (ADR 0010): where they go and what they carry."""

import logging

import pytest

from transaction_analytics.log_config import APP_LOGGER, configure_logging


def test_app_records_are_written_with_time_level_and_logger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")

    logging.getLogger(f"{APP_LOGGER}.api.errors").info("Client disconnected during POST /x")

    (line,) = capsys.readouterr().out.splitlines()
    # e.g. "2026-10-23 10:00:00,123 INFO transaction_analytics.api.errors Client disconnected ..."
    assert line.endswith(
        " INFO transaction_analytics.api.errors Client disconnected during POST /x"
    )
    assert line[:4].isdigit()


def test_records_below_the_level_are_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    logger = logging.getLogger(f"{APP_LOGGER}.services.imports")

    logger.info("not shown")
    logger.warning("shown")

    (line,) = capsys.readouterr().out.splitlines()
    assert line.endswith(" WARNING transaction_analytics.services.imports shown")


def test_configuring_twice_writes_each_record_once(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    configure_logging("INFO")

    logging.getLogger(APP_LOGGER).warning("once")

    assert len(capsys.readouterr().out.splitlines()) == 1


def test_other_loggers_are_left_alone(capsys: pytest.CaptureFixture[str]) -> None:
    root_handlers = list(logging.getLogger().handlers)

    configure_logging("DEBUG")
    logging.getLogger("somebody.else").warning("not ours")

    assert logging.getLogger().handlers == root_handlers
    assert "not ours" not in capsys.readouterr().out


def test_sqlalchemy_engine_parameters_stay_out_of_the_log() -> None:
    # At INFO the engine logs statement parameters: descriptions and amounts.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)

    configure_logging("DEBUG")

    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
