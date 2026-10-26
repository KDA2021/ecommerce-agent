"""Comparison bookkeeping must preserve paired tasks and honest failure counts."""
import pytest


def test_schedule_balances_route_order_and_keeps_every_pair():
    from scripts.eval_shopping import build_schedule

    schedule = build_schedule(['a', 'b'], 2)
    assert len(schedule) == 8
    for repeat in (1, 2):
        for task in ('a', 'b'):
            pair = [row for row in schedule if row['task_id'] == task and row['repetition'] == repeat]
            assert {row['route'] for row in pair} == {'direct', 'llm'}
    assert schedule[0]['route'] != schedule[2]['route']
    assert schedule[0]['route'] != schedule[4]['route']


@pytest.mark.parametrize('ids,repeats', [([], 1), (['a', 'a'], 2), (['a'], 0), (['a'], -1)])
def test_schedule_rejects_empty_duplicates_and_invalid_repeat_count(ids, repeats):
    from scripts.eval_shopping import build_schedule

    with pytest.raises(ValueError):
        build_schedule(ids, repeats)


def test_summary_counts_whole_task_and_keeps_failed_latency():
    from scripts.eval_shopping import summarize_runs

    runs = [
        {'task_id':'a','repetition':1,'route':'direct','category':'simple','task_passed':True,'latency_seconds':2,'llm_calls':1,'error':''},
        {'task_id':'a','repetition':1,'route':'llm','category':'simple','task_passed':False,'latency_seconds':8,'llm_calls':3,'error':''},
        {'task_id':'b','repetition':1,'route':'direct','category':'complex','task_passed':False,'latency_seconds':4,'llm_calls':2,'error':'provider failed'},
        {'task_id':'b','repetition':1,'route':'llm','category':'complex','task_passed':True,'latency_seconds':6,'llm_calls':4,'error':''},
    ]
    summary = summarize_runs(runs)
    assert summary['arms']['direct']['task_success_rate'] == 0.5
    assert summary['arms']['direct']['latency_mean_seconds'] == 3
    assert summary['arms']['llm']['latency_p95_seconds'] == 8
    assert summary['pairs']['planner_only_success'] == 1
    assert summary['pairs']['direct_only_success'] == 1
    assert summary['pairs']['paired_count'] == 2
    assert summary['arms']['direct']['error_count'] == 1


def test_resume_rejects_mismatched_source_catalog_or_tasks():
    from scripts.eval_shopping import validate_resume

    expected = {'source_fingerprint':'code-a','catalog_sha256':'catalog-a','tasks_sha256':'tasks-a','model':'model-a','repeats':2}
    validate_resume(expected, dict(expected))
    for name in expected:
        changed = {**expected, name:'different'}
        with pytest.raises(ValueError):
            validate_resume(expected, changed)


@pytest.mark.parametrize('key', ['effective_config_sha256', 'knowledge_sha256'])
def test_resume_rejects_changed_provider_execution_or_knowledge(key):
    from scripts.eval_shopping import validate_resume

    expected = {'source_fingerprint':'s','catalog_sha256':'c','tasks_sha256':'t','model':'m','repeats':2,
                'effective_config_sha256':'config-a','knowledge_sha256':'kb-a'}
    with pytest.raises(ValueError):
        validate_resume(expected, {**expected, key:'changed'})


def test_catalog_contains_unique_model_aliases_and_actual_sku_prices():
    from app.infrastructure.persistence.seed_products import build_seed_products
    from scripts.eval_shopping import catalog_snapshot

    catalog = catalog_snapshot(build_seed_products())
    assert 'VoltTrek 65W' in catalog['P1005']['aliases']
    assert 'VoltTrek65W' in catalog['P1005']['aliases']
    assert {row['amount'] for row in catalog['P1001']['sku_prices']} == {'189.00','199.00'}


def test_judge_errors_are_separate_from_agent_execution_errors():
    from scripts.eval_shopping import summarize_runs

    runs = [{'task_id':'a','repetition':1,'route':'direct','category':'simple','task_passed':False,
             'latency_seconds':2,'llm_calls':1,'error':'','turns':[{'error':'','grading':{
                 'passed':False,'failures':['judge_error: TimeoutError']}}]}]
    arm = summarize_runs(runs)['arms']['direct']
    assert arm['judge_error_count'] == 1
    assert arm['agent_error_count'] == 0
