import pytest

from transaction_analytics.domain.csv_parser import parse_statement_csv
from transaction_analytics.domain.errors import FileErrorCode, FileRejectedError
from transaction_analytics.domain.imports import (
    ImportFailureCode,
    ImportOutcome,
    ImportStatus,
    completed_outcome,
    internal_error_outcome,
    rejected_outcome,
)

HEADER = b"date,description,amount,currency\n"


def test_failure_codes_are_every_file_error_plus_internal_error() -> None:
    file_codes = {code.value for code in FileErrorCode}
    failure_codes = {code.value for code in ImportFailureCode}

    assert failure_codes - file_codes == {"internal_error"}
    assert file_codes <= failure_codes


def test_completed_outcome_keeps_partial_results() -> None:
    result = parse_statement_csv(HEADER + b"2026-09-01,a,1.00,GBP\n2026-02-30,b,1.00,GBP\n")

    outcome = completed_outcome(result)

    assert outcome == ImportOutcome(
        status=ImportStatus.COMPLETED, total_rows=2, valid_rows=1, invalid_rows=1
    )


def test_completed_outcome_has_no_error_threshold() -> None:
    result = parse_statement_csv(HEADER + b"2026-02-30,a,1.00,GBP\n")

    outcome = completed_outcome(result)

    assert outcome.status is ImportStatus.COMPLETED
    assert (outcome.valid_rows, outcome.invalid_rows) == (0, 1)


@pytest.mark.parametrize("code", list(FileErrorCode))
def test_rejected_outcome_maps_every_file_error(code: FileErrorCode) -> None:
    outcome = rejected_outcome(FileRejectedError(code, "Fixed message."))

    assert outcome.status is ImportStatus.FAILED
    assert outcome.error_code == ImportFailureCode(code.value)
    assert outcome.error_message == "Fixed message."
    assert (outcome.total_rows, outcome.valid_rows, outcome.invalid_rows) == (0, 0, 0)


def test_internal_error_outcome() -> None:
    outcome = internal_error_outcome()

    assert outcome.status is ImportStatus.FAILED
    assert outcome.error_code is ImportFailureCode.INTERNAL_ERROR
    assert outcome.error_message
