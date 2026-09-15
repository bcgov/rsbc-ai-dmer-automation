"""Unit tests for the Document Intelligence client wrapper (mocked SDK).

Behaviour specs (GIVEN/WHEN/THEN) for result normalization, retry on transient
failure, and circuit-breaker rejection.
"""

from __future__ import annotations

import pytest

from dmer_common.doc_intelligence import DIResult, DocumentIntelligenceClient
from dmer_common.retry import CircuitBreaker, CircuitOpenError


class _FakePoller:
    def __init__(self, result) -> None:
        self._result = result

    def result(self):
        return self._result


class _FakeSdkClient:
    """Mock DI SDK client capturing calls and returning a canned AnalyzeResult."""

    def __init__(self, result_dict, *, fail_times: int = 0) -> None:
        self._result_dict = result_dict
        self._fail_times = fail_times
        self.calls: list[dict] = []

    def begin_analyze_document(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("transient DI error")

        class _Result:
            def __init__(self, d):
                self._d = d

            def as_dict(self):
                return self._d

        return _FakePoller(_Result(self._result_dict))


def test_analyze_normalizes_sdk_result():
    # GIVEN a mocked DI client returning content/pages/documents
    sdk = _FakeSdkClient(
        {
            "content": "hello",
            "pages": [{"pageNumber": 1}],
            "documents": [{"fields": {}}],
        }
    )
    client = DocumentIntelligenceClient(sdk_client=sdk)
    # WHEN analyzing page 1 with a custom model
    result = client.analyze("custom-dmer-top-level", b"%PDF-bytes", pages="1")
    # THEN a normalized DIResult is returned and the model/pages were passed through
    assert isinstance(result, DIResult)
    assert result.content == "hello"
    assert result.pages == [{"pageNumber": 1}]
    assert sdk.calls[0]["model_id"] == "custom-dmer-top-level"
    assert sdk.calls[0]["pages"] == "1"


def test_analyze_retries_transient_failures():
    # GIVEN a client that fails once then succeeds
    sdk = _FakeSdkClient({"content": "ok"}, fail_times=1)
    client = DocumentIntelligenceClient(sdk_client=sdk)
    # WHEN analyzing THEN the retry policy recovers and returns the result
    result = client.analyze("prebuilt-read", b"img")
    assert result.content == "ok"
    assert len(sdk.calls) == 2


def test_analyze_rejects_when_breaker_open():
    # GIVEN a client whose breaker is already open
    sdk = _FakeSdkClient({"content": "x"})
    breaker = CircuitBreaker(failure_threshold=1)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("trip")))
    client = DocumentIntelligenceClient(sdk_client=sdk, breaker=breaker)
    # WHEN analyzing THEN it fails fast without calling the SDK
    with pytest.raises(CircuitOpenError):
        client.analyze("prebuilt-read", b"img")
    assert sdk.calls == []


def test_constructor_requires_endpoint_without_sdk_client():
    # GIVEN neither endpoint nor sdk_client
    # WHEN constructing THEN it raises
    with pytest.raises(ValueError):
        DocumentIntelligenceClient()
