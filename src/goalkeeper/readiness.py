"""Fresh offline evidence is required before any paid four-product run."""
import json
from pathlib import Path

from .paths import PROJECT, inside_project
from .storage import fingerprint, snapshot

REQUIRED_TESTS = {
    "test_single_runtime_architecture_and_shared_entry_factories",
    "test_rejected_worker_cannot_overwrite_active_process_identity",
    "test_legacy_state_read_is_lossless_and_does_not_rewrite_history",
    'test_sequential_turns_do_not_leak_or_reset_history_deadline',
    'test_pinned_codex_consumes_four_sequential_turns_without_inference',
    'test_actual_native_products_consume_four_sequential_local_turns[pi]',
    'test_actual_native_products_consume_four_sequential_local_turns[opencode]',
    'test_workspace_root_scope_allows_atomic_edits_and_denies_other_trees',
    'test_remove_occ_grader_accepts_reference_rejects_wrong_all', 
    'test_failed_response_with_wrong_model_cannot_retry_or_publish',
    'test_recovered_delivery_and_usage_completeness_are_independent',
    'test_each_request_can_recover_and_unknown_usage_stays_visible',
    'test_exhausted_group_does_not_stop_other_three_or_their_watcher',
    'test_four_business_packages_save_with_actual_pinned_codex_permissions',
    'test_failed_or_wrong_model_response_cannot_be_misclassified_as_recovery',
    'test_pinned_codex_recovers_http_503_without_a_blocked_native_turn',
    'test_progress_flood_yields_to_peers_and_writes_linear_control_evidence',
    'test_outer_cancel_waits_for_all_four_job_cleanup',
    'test_copy_failure_cannot_publish_or_claim_delivery',
    'test_authentication_failure_before_send_is_known_zero_and_not_retried',
    'test_scoped_pinned_codex_permissions_without_inference',
    'test_pinned_codex_consumes_slow_staged_response_without_inference',
    'test_frozen_c1_grader_accepts_reference_and_rejects_critical_errors',
    'test_pinned_codex_accepts_feedback_after_prior_turn_completed',
    'test_c1_v2_public_private_contract_and_critical_mutations',
    'test_summary_failure_breaker_survives_changed_history_and_resume',
    'test_summary_real_tool_refs_and_encrypted_archive',
    'test_summary_failed_correction_still_allows_verified_delivery',
    'test_unlimited_forwarding_records_usage_without_token_stop',
    'test_unlimited_summary_uses_task_time_without_cost_cutoff',
    'test_pinned_codex_goal_has_no_token_budget',
    'test_longchain_reference_public_private_and_mutations',
    'test_longchain_feedback_delivery_and_quality',
    'test_highchain_reference_public_private_and_mutations',
    'test_highchain_feedback_delivery_and_quality',
    'test_business_reference_contracts_and_critical_mutations',
    'test_business_feedback_delivery_and_quality',
    'test_business_artifacts_under_actual_task_permissions',
    'test_edit_many_all_or_nothing_and_preserves_file_mode',
    'test_eight_known_edits_batch_to_one_round_with_same_verified_delivery',
    'test_context_calibration_retains_opaque_bytes_and_wire_budget_forecast',
    'test_calibrated_opaque_history_does_not_trigger_premature_summary',
    'test_feedback_appends_context_without_rewriting_prefix_and_survives_reload',
    'test_command_preview_preserves_status_failure_tail_and_archive',
    'test_provider_emits_stable_role_cache_key_and_batch_flag_without_inference',
}


def package_identity():
    return {name: fingerprint(snapshot(PROJECT/'evaluation/private'/name))
            for name in ('connect', 'C1-v2', 'L1-v1', 'H1-v1', 'B1-v1', 'B3-v1', 'B6-v1', 'B11-v1', 'B1-v2', 'B3-v2', 'B6-v2', 'B11-v2', *sorted(p.name for p in (PROJECT/'evaluation/private').glob('SB10-*') if p.is_dir()))}


def test_identity():
    return fingerprint({str(p.relative_to(PROJECT)): p.read_bytes().hex()
        for p in (PROJECT/'tests').rglob('*.py')})


def require_offline_readiness(identity, path=None, package=None):
    path = inside_project(path or PROJECT/'outputs/full-code-audit/offline-readiness.json')
    try:
        proof = json.loads(path.read_text())
        if not isinstance(proof, dict):
            raise ValueError()
        if (proof.get('kind') != 'offline_readiness' or proof.get('passed') is not True
                or identity not in proof.get('execution_identities', [])
                or proof.get('tests_hash') != test_identity()
                or proof.get('packages') != package_identity()
                or not REQUIRED_TESTS.issubset(set(proof.get('verified_cases', [])))):
            raise ValueError()
        for evidence in proof.get('evidence', []):
            file = inside_project(Path(evidence['path']))
            if fingerprint(file.read_text()) != evidence['hash']:
                raise ValueError()
        if len(proof.get('evidence', [])) != 2:
            raise ValueError()
        if package is not None:
            package = inside_project(package)
            name = package.name
            if (package != PROJECT/'evaluation/private'/name
                    or name not in proof['packages']
                    or fingerprint(snapshot(package)) != proof['packages'][name]):
                raise ValueError()
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError('缺少当前版本完整离线验收记录，禁止调用真实模型；先运行 scripts/check_offline.py。') from error
    return proof
