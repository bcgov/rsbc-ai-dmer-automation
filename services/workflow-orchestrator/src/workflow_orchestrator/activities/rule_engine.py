"""Rule Engine Durable activity: ruleset and artifact I/O, evaluation, persistence.

See docs/development/stages/05-activity-rule-engine.md. The evaluation itself
is ``dmer_common.rules`` (GoRules ZEN, in-process); the DB unit of work is
``dmer_common.db.rule_engine``. Clinical content stays in the blob and the
``rule_evaluation`` row -- the activity returns and logs only the outcome
code, the rules version and ids.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

import azure.durable_functions as df
from dmer_common.db.rule_engine import (
    RuleEngineBusyError,
    RuleEngineNotReadyError,
    RuleEngineStateError,
    rule_engine_session,
)
from dmer_common.rules import RuleEvaluationError, Ruleset, RulesetError
from dmer_common.storage import BlobClient, rules, rules_active_path
from dmer_common.telemetry import document_id_context, get_logger

from ._runtime import get_async_engine as _get_async_engine
from ._runtime import poison_as_result
from ._runtime import run_sync as _run_sync

_log = get_logger(__name__)

ACTIVITY_NAME = "RunRuleEngine"


class RuleEngineValidationError(ValueError):
    """Poison for this document: bad input, an unreadable normalized artifact,
    or a document the rules can't evaluate. Retrying cannot fix it."""


class RulesetUnavailableError(RuntimeError):
    """The active rules.json is missing or invalid -- poison for every document
    in flight, not just this one. Logged at ERROR for alerting."""


class RuleEngineActivityError(RuntimeError):
    """Operational failure; the document orchestrator owns bounded retries."""


# Compiled rulesets by checksum: the active ruleset is re-read every
# invocation (so a pointer swap takes effect immediately), but only compiled
# once per worker process.
_RULESETS: dict[str, Ruleset] = {}


def _input(payload: dict) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise RuleEngineValidationError("Rule Engine input must be an object")
    document_id = payload.get("document_id")
    blob_url = payload.get("normalized_blob_url")
    try:
        if not isinstance(document_id, str):
            raise TypeError
        document_id = str(UUID(document_id))
    except (ValueError, TypeError):
        raise RuleEngineValidationError(
            "Rule Engine input requires a document UUID"
        ) from None
    parsed = urlparse(blob_url if isinstance(blob_url, str) else "")
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.netloc
        or not parsed.path.strip("/")
    ):
        raise RuleEngineValidationError(
            "Rule Engine input requires a normalized blob URL"
        )
    return document_id, blob_url


def _normalized_document(content: bytes) -> dict:
    try:
        dmer = json.loads(content)
    except (ValueError, UnicodeError):
        raise RuleEngineValidationError("Invalid normalized artifact") from None
    if not isinstance(dmer, dict):
        raise RuleEngineValidationError("Invalid normalized artifact")
    return dmer


def _ruleset(content: bytes) -> Ruleset:
    try:
        ruleset = Ruleset(content)
    except RulesetError:
        raise RulesetUnavailableError("Active rules.json is invalid") from None
    return _RULESETS.setdefault(ruleset.checksum, ruleset)


def _completed(evaluation_id: int, version: str, outcome: str) -> dict:
    return {
        "rule_evaluation_id": evaluation_id,
        "rules_version": version,
        "selected_outcome_code": outcome,
    }


async def run_rule_engine_activity(payload: dict) -> dict:
    """Evaluate the normalized document and persist every candidate outcome.

    Input: ``{"document_id", "normalized_blob_url"}``. Returns the
    ``rule_evaluation`` id, rules version and selected outcome code -- also on
    a retried invocation after a committed success, without re-evaluating.
    """
    document_id, normalized_blob_url = _input(payload)
    engine = None
    with document_id_context(document_id):
        _log.info("rule engine: triggered")
        try:
            engine = await _get_async_engine()
            async with rule_engine_session(engine, document_id) as repository:
                run = await repository.start_or_resume(now=datetime.now(UTC))
                if run.completed is not None:
                    _log.info("rule engine: returning committed evaluation")
                    done = run.completed
                    return _completed(
                        done.rule_evaluation_id,
                        done.rules_version,
                        done.selected_outcome_code,
                    )
                try:
                    if run.received_date is None:
                        # The monocular rules measure the DMER's age from it.
                        raise RuleEngineValidationError("Document has no received date")
                    blob = BlobClient(os.environ["BLOB_ACCOUNT_URL"])
                    rules_url = blob.blob_url(rules(), rules_active_path())
                    try:
                        rules_content = await _run_sync(blob.download, rules_url)
                    # Any read failure means there is no active ruleset.
                    except Exception:  # noqa: BLE001
                        raise RulesetUnavailableError(
                            "Active rules.json could not be read"
                        ) from None
                    ruleset = _ruleset(rules_content)
                    version = await repository.register_rules_version(
                        checksum=ruleset.checksum,
                        blob_url=rules_url,
                        now=datetime.now(UTC),
                    )
                    content = await _run_sync(blob.download, normalized_blob_url)
                    dmer = _normalized_document(content)
                    try:
                        evaluation = ruleset.evaluate(
                            dmer, received_date=run.received_date
                        )
                    except RuleEvaluationError:
                        raise RuleEngineValidationError(
                            "Rules could not evaluate the normalized document"
                        ) from None
                    evaluation_id, count = await repository.succeed(
                        run.run_id,
                        evaluation=evaluation,
                        rules_version_name=version,
                        driver_key=run.driver_key,
                        ended_at=datetime.now(UTC),
                    )
                    _log.info(
                        "rule engine: succeeded",
                        extra={
                            "run_id": run.run_id,
                            "rules_version": version,
                            "selected_outcome_code": evaluation.selected_outcome_code,
                            "rule_engine_outcome_code": evaluation.rule_engine_outcome_code,
                            "candidate_count": len(evaluation.outcomes),
                            "driver_count_incremented": count.counted,
                            "open_driver_evaluation": count.driver_evaluation_id
                            is not None,
                        },
                    )
                    if count.driver_evaluation_id is None:
                        # Resolve Driver creates the evaluation first; without
                        # one the document cannot join a driver batch.
                        _log.warning(
                            "rule engine: no open driver evaluation; document not counted"
                        )
                    return _completed(
                        evaluation_id, version, evaluation.selected_outcome_code
                    )
                except Exception as exc:
                    try:
                        await repository.fail(
                            run.run_id,
                            ended_at=datetime.now(UTC),
                            error_code=type(exc).__name__,
                            error_detail="Rule evaluation attempt failed; see error_code.",
                        )
                    except Exception:  # noqa: BLE001 - keep the original failure
                        _log.error("rule engine: unable to record failed attempt")
                    raise
        except RulesetUnavailableError:
            _log.error("rule engine: active rules.json unavailable")
            raise
        except (
            RuleEngineValidationError,
            RuleEngineBusyError,
            RuleEngineNotReadyError,
            RuleEngineStateError,
        ):
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize before Durable serialization
            _log.error(
                "rule engine: operational failure",
                extra={"error_code": type(exc).__name__},
            )
            raise RuleEngineActivityError(
                "Rule Engine dependency or persistence failure"
            ) from None
        finally:
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception:  # noqa: BLE001 - cleanup must not replace a result
                    _log.error("rule engine: unable to dispose database engine")


# Errors retrying can't fix: returned as a poison result for the orchestrator
# to route to MANUAL_REVIEW, instead of being retried.
POISON = (RuleEngineValidationError, RuleEngineStateError)


def register(app: df.DFApp) -> None:
    """Register the Rule Engine activity on *app* (called once from
    function_app.py); kept out of import time like ``normalize.register``."""

    @app.function_name(name=ACTIVITY_NAME)
    @app.activity_trigger(input_name="payload")
    async def _run_rule_engine(payload: dict) -> dict:
        return await poison_as_result(run_rule_engine_activity(payload), POISON)
