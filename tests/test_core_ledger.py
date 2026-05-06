from core.ledger import ChronosLedger


def test_register_node_is_idempotent() -> None:
    ledger = ChronosLedger()
    ledger.register_node("node_a")
    ledger.register_node("node_a")

    assert ledger.get_balance("node_a") == 0.0
    assert len(ledger.accounts) == 1


def test_create_task_deducts_consumer_balance() -> None:
    ledger = ChronosLedger()
    consumer_id = "consumer_1"
    ledger.register_node(consumer_id)
    ledger.accounts[consumer_id] = 12.0

    task_id = ledger.create_task(consumer_id, "work", 5.0)

    assert ledger.get_balance(consumer_id) == 7.0
    assert ledger.tasks[task_id]["status"] == "pending"
    assert ledger.tasks[task_id]["bounty"] == 5.0


def test_create_task_rejects_when_balance_insufficient() -> None:
    ledger = ChronosLedger()
    consumer_id = "consumer_2"
    ledger.register_node(consumer_id)
    ledger.accounts[consumer_id] = 1.0

    try:
        ledger.create_task(consumer_id, "too expensive", 2.0)
        raise AssertionError("Expected ValueError for insufficient balance")
    except ValueError as exc:
        assert "Insufficient SECONDS balance" in str(exc)


def test_submit_result_pays_provider_and_marks_completed() -> None:
    ledger = ChronosLedger()
    consumer_id = "consumer_3"
    provider_id = "provider_3"
    ledger.register_node(consumer_id)
    ledger.accounts[consumer_id] = 10.0
    task_id = ledger.create_task(consumer_id, "task", 3.0)

    ok = ledger.submit_result(task_id, provider_id, "done")

    assert ok is True
    assert ledger.tasks[task_id]["status"] == "completed"
    assert ledger.tasks[task_id]["provider"] == provider_id
    assert ledger.get_balance(provider_id) == 3.0
    assert ledger.get_balance(consumer_id) == 7.0


def test_submit_result_rejects_when_task_not_pending() -> None:
    ledger = ChronosLedger()
    consumer_id = "consumer_4"
    provider_id = "provider_4"
    ledger.register_node(consumer_id)
    ledger.accounts[consumer_id] = 10.0
    task_id = ledger.create_task(consumer_id, "task", 4.0)
    assert ledger.submit_result(task_id, provider_id, "done") is True

    second_try = ledger.submit_result(task_id, provider_id, "done again")

    assert second_try is False
