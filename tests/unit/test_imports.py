from transaction_analytics.domain.errors import FileErrorCode
from transaction_analytics.domain.imports import ImportFailureCode


def test_failure_codes_are_every_file_error_plus_internal_error() -> None:
    file_codes = {code.value for code in FileErrorCode}
    failure_codes = {code.value for code in ImportFailureCode}

    assert failure_codes - file_codes == {"internal_error"}
    assert file_codes <= failure_codes
