"""Rule Engine Durable activity: ruleset and artifact I/O, evaluation, persistence.

See docs/development/stages/05-activity-rule-engine.md. ``DECISION_ENGINE``
chooses what evaluates the normalized DMER:

- ``rules`` (default, and what every environment runs): ``rules.json`` with
  GoRules ZEN, in-process (``dmer_common.rules``).
- ``triage_llm``: the triage LLM against the Triage Sort Procedures PDF
  (``dmer_common.triage``); its full answer is stored beside the normalized
  output. Off unless set explicitly -- kept so it can be re-evaluated, e.g.
  with a newer model.

Either way the outcome is picked by the rules engine's ``select_outcome`` and
recorded the same way (``dmer_common.db.rule_engine``): the same
``rule_evaluation`` row, stage run and driver count. Clinical content stays in
the blob and the ``rule_evaluation`` row -- the activity returns and logs only
the outcome code, the version and ids.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

import azure.durable_functions as df
from dmer_common.config import openai_settings
from dmer_common.db.rule_engine import (
    RuleEngineBusyError,
    RuleEngineNotReadyError,
    RuleEngineStateError,
    rule_engine_session,
)
from dmer_common.normalization import NormalizationValidationError
from dmer_common.openai_client import OpenAIClient
from dmer_common.rules import RuleEvaluation, RuleEvaluationError, Ruleset, RulesetError
from dmer_common.storage import (
    BlobClient,
    normalized_dmer,
    rules,
    rules_active_path,
    triage_path,
)
from dmer_common.telemetry import document_id_context, get_logger
from dmer_common.triage import (
    PolicyTextError,
    PolicyUnavailableError,
    load_policy,
    triage_document,
)

from ._runtime import get_async_engine as _get_async_engine
from ._runtime import poison_as_result
from ._runtime import run_sync as _run_sync

_log = get_logger(__name__)

ACTIVITY_NAME = "RunRuleEngine"

TRIAGE_LLM = "triage_llm"
RULES = "rules"
DECISION_ENGINES = (TRIAGE_LLM, RULES)

# The triage LLM's evaluation date (the procedures' "today"): BC's.
_EVALUATION_TZ = ZoneInfo("America/Vancouver")


class RuleEngineValidationError(ValueError):
    """Poison for this document: bad input, an unreadable normalized artifact,
    or a document the rules can't evaluate. Retrying cannot fix it."""


class RulesetUnavailableError(RuntimeError):
    """The decision engine can't run for any document: the active rules.json
    (or, for the triage LLM, the procedures PDF) is missing, invalid, or not
    registered in rules_version, or DECISION_ENGINE is not an engine --
    affects every document in flight, not just this one. Logged at ERROR for
    alerting."""


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


def _decision_engine() -> str:
    # Unset means the rules engine: the triage LLM only runs when chosen.
    engine = os.environ.get("DECISION_ENGINE", RULES).strip().lower()
    if engine not in DECISION_ENGINES:
        _log.error("rule engine: DECISION_ENGINE is not triage_llm or rules")
        raise RulesetUnavailableError("DECISION_ENGINE must be triage_llm or rules")
    return engine


async def _evaluate_rules(
    repository, blob: BlobClient, normalized_blob_url: str, received_date: datetime
) -> tuple[RuleEvaluation, str]:
    """rules.json (GoRules ZEN): the evaluation and the registered rules version."""
    rules_url = blob.blob_url(rules(), rules_active_path())
    try:
        rules_content = await _run_sync(blob.download, rules_url)
    # Any read failure means there is no active ruleset.
    except Exception:  # noqa: BLE001
        raise RulesetUnavailableError("Active rules.json could not be read") from None
    ruleset = _ruleset(rules_content)
    # rules_version is written by maintenance, never here.
    version = await repository.find_rules_version(checksum=ruleset.checksum)
    if version is None:
        _log.error(
            "rule engine: active rules.json has no registered rules_version",
            extra={"checksum": ruleset.checksum},
        )
        raise RulesetUnavailableError(
            "Active rules.json is not registered in rules_version"
        )
    content = await _run_sync(blob.download, normalized_blob_url)
    dmer = _normalized_document(content)
    try:
        evaluation = ruleset.evaluate(dmer, received_date=received_date)
    except RuleEvaluationError:
        raise RuleEngineValidationError(
            "Rules could not evaluate the normalized document"
        ) from None
    return evaluation, version


async def _evaluate_triage(
    repository,
    blob: BlobClient,
    normalized_blob_url: str,
    document_id: str,
    run_id: int,
) -> tuple[RuleEvaluation, str, dict]:
    """The triage LLM: the evaluation, the procedures version, and the decision
    record for ``all_outcomes``. Its full answer goes to the blob, never the
    database or the logs (it quotes the DMER)."""
    try:
        policy = await _run_sync(load_policy, blob)
    except (PolicyUnavailableError, PolicyTextError) as exc:
        _log.error(
            "rule engine: procedures PDF unavailable",
            extra={"error_code": type(exc).__name__},
        )
        raise RulesetUnavailableError(
            "Active procedures PDF is unavailable or its text does not verify"
        ) from None
    version = policy.updated.isoformat() if policy.updated else None
    # Like rules.json: the procedures' version is registered by maintenance.
    if version is None or not await repository.rules_version_registered(version):
        _log.error(
            "rule engine: procedures PDF version not registered in rules_version",
            extra={"pdf_sha256": policy.pdf_sha256, "version": version},
        )
        raise RulesetUnavailableError(
            "Active procedures PDF's version is not registered in rules_version"
        )
    content = await _run_sync(blob.download, normalized_blob_url)
    dmer = _normalized_document(content)
    try:
        result = await _run_sync(
            triage_document,
            OpenAIClient(settings=openai_settings()),
            dmer,
            policy,
            evaluation_date=datetime.now(_EVALUATION_TZ).date(),
        )
    except NormalizationValidationError:
        raise RuleEngineValidationError(
            "Triage answer still invalid after its retries"
        ) from None
    record = result.record()
    answer_url = await _run_sync(
        blob.upload_json,
        normalized_dmer(),
        triage_path(document_id, run_id=run_id),
        {"record": record, "answer": result.answer},
    )
    return result.evaluation, version, {**record, "answer_blob_url": answer_url}


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
                    engine_name = _decision_engine()
                    if engine_name == TRIAGE_LLM:
                        evaluation, version, record = await _evaluate_triage(
                            repository,
                            blob,
                            normalized_blob_url,
                            document_id,
                            run.run_id,
                        )
                    else:
                        evaluation, version = await _evaluate_rules(
                            repository, blob, normalized_blob_url, run.received_date
                        )
                        record = None
                    evaluation_id, count = await repository.succeed(
                        run.run_id,
                        evaluation=evaluation,
                        rules_version_name=version,
                        driver_key=run.driver_key,
                        ended_at=datetime.now(UTC),
                        all_outcomes=record,
                    )
                    _log.info(
                        "rule engine: succeeded",
                        extra={
                            "decision_engine": engine_name,
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
                        # Driver Lookup creates the evaluation first; without
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
