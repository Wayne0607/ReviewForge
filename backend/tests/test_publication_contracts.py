from dataclasses import replace

from test_proof_gate import candidate, ledger, observation
from test_source_audit import audit

from reviewforge.engine.hypothesis import Mechanism, Site
from reviewforge.engine.publication_contracts import contract_catalog


def test_java_throws_declaration_is_not_an_exclusive_exception_consumer_contract():
    item = candidate()
    item.mechanism = Mechanism.ERROR_PATH
    item.claim = "Throws RuntimeException instead of TaskException"
    item.sites = [Site("Task.java", 1, "execute")]
    item.observations = [
        replace(observation(), path="Task.java", excerpt="public void execute() throws TaskException { task.run(); }")
    ]
    book = ledger(item, protocol=4)
    book.source_audits[item.id] = {
        "facts": {
            **audit(),
            "contracts": [
                {"kind": "caller", "statement": "All errors must be TaskException", "evidence": ["h_bug/obs_0:e1"]}
            ],
        }
    }
    assert not contract_catalog(item, book)
    # A real type-sensitive consumer is different from a declared throws list.
    item.observations[0] = replace(
        item.observations[0], excerpt="try { task.run(); } catch (TaskException e) { recover(); }"
    )
    assert contract_catalog(item, book)


def test_catch_text_in_a_java_comment_is_not_a_consumer():
    item = candidate()
    item.mechanism = Mechanism.ERROR_PATH
    item.claim = "Throws RuntimeException instead of TaskException"
    item.sites = [Site("Task.java", 1, "execute")]
    item.observations = [
        replace(
            observation(),
            path="Task.java",
            excerpt="// catch (TaskException e) { recover(); }\n"
            "public void execute() throws TaskException { task.run(); }",
        )
    ]
    book = ledger(item, protocol=4)
    book.source_audits[item.id] = {
        "facts": {"contracts": [{"kind": "caller", "statement": "Requirement", "evidence": ["h_bug/obs_0:e1"]}]}
    }
    assert not contract_catalog(item, book)


def test_locale_rule_binds_actual_target_resource_without_inventing_a_caller():
    item = candidate()
    item.mechanism = Mechanism.I18N
    path = "theme/messages_lt.properties"
    item.sites = [Site(path, 1, "hello=ciao")]
    item.observations = [replace(observation(), path=path, excerpt="hello=ciao")]
    book = ledger(item, protocol=4)
    book.source_audits[item.id] = {"facts": {"contracts": []}}
    contracts = contract_catalog(item, book)
    assert len(contracts) == 1
    contract = next(iter(contracts.values()))
    assert contract["origin"] == "program-bound-localization-rule" and "locale lt" in contract["statement"]
    item.sites = [Site("theme/messages_en.properties", 1, "hello=ciao")]
    assert not contract_catalog(item, book)  # A neighbor locale is not this target's contract.
