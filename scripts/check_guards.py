"""Prove each listed guard is tested: disabling it must turn its tests red.

Copies the working tree into a temporary, self-contained Git repository, disables
one guard at a time, and runs only that guard's tests. A new guard adds an entry.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# (file, guard text, disabled replacement, tests that must fail without it)
GUARDS = [
    ('soso/workspace.py', 'any(p.casefold() == ".git" for p in path.parts)', '".git" in path.parts',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_git_dir_is_rejected_in_any_case']),
    ('soso/workspace.py', 'name = PurePosixPath(name).as_posix()', 'name = name',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_protection_matches_normalised_names']),
    ('soso/workspace.py', 'raise PermissionError("writes through symlinks are forbidden")', 'pass',
     ['test_core.WorkspaceTests.test_internal_symlink_is_not_writable']),
    ('soso/workspace.py', 'return bool(self.protection.match(name) or self.protection.match(name.casefold()))',
     'return any(fnmatch.fnmatchcase(name, p) for p in self.protected)', ['test_sandbox_integrity.SandboxIntegrityTests.test_protection_ignores_case']),
    ('soso/workspace.py', 'self.protection.match(name) or ', '',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_protection_keeps_case_sensitive_character_classes']),
    ('soso/workspace.py', ' or self.protection.match(name.casefold())', '',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_protection_keeps_positive_character_classes_as_written']),
    ('soso/workspace.py', '"(?i:" + re.escape(c) + ")"', 're.escape(c)',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_protection_keeps_positive_character_classes_as_written']),
    ('soso/workspace.py', 'fnmatch.translate(pattern[i:j + 1])', 'fnmatch.translate(pattern[i:j + 1].casefold())',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_protection_keeps_positive_character_classes_as_written']),
    ('soso/workspace.py', 'raise PermissionError("submodule paths are not writable")', 'pass',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_tool_refuses_writes_inside_a_base_submodule']),
    ('soso/workspace.py', 'path.name.casefold() == "agents.md"', 'path.name == "AGENTS.md"',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_agents_md_name_check_ignores_case']),
    ('soso/engine.py', 'Path(name).name.casefold() == "agents.md"', 'Path(name).name == "AGENTS.md"',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_agents_md_name_check_ignores_case']),
    ('soso/engine.py', 'if any(mode == "160000" for mode in modes):', 'if False:',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_adding_embedded_repository_is_invalid']),
    ('soso/engine.py', 'elif any(any((path / link).iterdir()) for link in gitlinks(path) if (path / link).is_dir()):', 'elif False:',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_with_files_inside_a_submodule_directory_is_invalid']),
    ('soso/engine.py', 'elif nested_git(path) != nested:', 'elif False:',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_hiding_files_in_a_nested_git_entry_is_invalid']),
    ('soso/engine.py', 'changed = self.restore_watched(path, own, restored)', 'changed = []',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_sibling_config_change_does_not_invalidate_an_innocent_candidate']),
    ('soso/engine.py', 'bool(foreign) and staged or ', '', ['test_sandbox_integrity.SandboxIntegrityTests.test_commit_made_under_a_sibling_filter_is_invalid']),
    ('soso/engine.py', 'any(key.startswith("worktree:") for key in changed)', 'False',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_own_worktree_config_change_is_invalid_even_beside_siblings']),
    ('soso/engine.py', '\n                            or bool(changed and alone))', ')',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_changing_repository_config_is_invalid_and_config_is_restored',
      'test_sandbox_integrity.SandboxIntegrityTests.test_candidate_hiding_edits_with_an_fsmonitor_hook_is_invalid']),
    ('soso/engine.py', 'alone = alone and self.started_experiments == sequence + 1', 'alone = alone',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_sibling_config_change_does_not_invalidate_an_innocent_candidate']),
    ('soso/engine.py', 'if guarded:\n                    self.restore_watched(path, own, restored)\n                restores = self.restores',
     'if guarded:\n                    pass\n                restores = self.restores',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_sibling_change_after_the_commit_is_restored_before_evaluation']),
    ('soso/engine.py', 'with contextlib.suppress(RuntimeError):\n                        self.restore_watched(path, own, restored)',
     'with contextlib.suppress(RuntimeError):\n                        pass',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_watched_config_change_is_restored_when_the_worker_fails']),
    ('soso/engine.py', 'git(path, "config", *where, "--unset-all", key, ok=(0, 5))', 'pass',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_changing_repository_config_is_invalid_and_config_is_restored']),
    ('soso/engine.py', 'git(path, "config", *where, "--add", key, value)', 'pass',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_watched_config_change_without_source_changes_is_restored']),
    ('soso/engine.py', 'problems.append((file, f"could not restore the {scope} Git configuration; see config_restored"))', 'pass',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_failed_restore_ends_the_candidate_with_an_error']),
    ('soso/engine.py', 'for key in dict.fromkeys(k for k, _ in recorded[0] + direct):\n'
     '                before, after = [v for k, v in recorded[0] if k == key], [v for k, v in direct if k == key]',
     'for key in dict.fromkeys(k for k, _ in recorded[1] + effective):\n'
     '                before, after = [v for k, v in recorded[1] if k == key], [v for k, v in effective if k == key]',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_failed_restore_of_an_included_setting_leaves_repository_config_untouched']),
    ('soso/engine.py', 'elif effective != recorded[1]:', 'elif False:',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_failed_restore_of_an_included_setting_leaves_repository_config_untouched',
      'test_evaluated_tree.EvaluatedTreeTests.test_include_activated_by_an_unwatched_setting_ends_the_candidate_and_blocks_new_runs']),
    ('soso/engine.py', 'raise RuntimeError("; ".join(message for _, message in problems))', 'pass', ['test_sandbox_integrity.SandboxIntegrityTests.test_failed_restore_of_an_included_setting_leaves_repository_config_untouched']),
    ('soso/engine.py', 'restored = list({change["key"]: change for change in reversed(restored)}.values())[::-1]', 'pass',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_failed_restore_ends_the_candidate_with_an_error']),
    ('soso/engine.py', 'env = {"GIT_INDEX_FILE": str(index)}', 'env = {}',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_commit_comparison_leaves_the_candidate_index_untouched']),
    ('soso/engine.py', '                self.attributes.unlink()', '                pass',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_changing_repository_config_is_invalid_and_config_is_restored']),
    ('soso/engine.py', 'self.attributes.write_bytes(self.watched[1])', 'None',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_watched_config_change_without_source_changes_is_restored']),
    ('soso/engine.py', 'r"filter\\..*|', 'r"', ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_changing_repository_config_is_invalid_and_config_is_restored']),
    ('soso/engine.py', 'include\\..*|includeif\\..*|', '', ['test_sandbox_integrity.SandboxIntegrityTests.test_filter_behind_an_include_is_invalid_and_restored']),
    ('soso/engine.py', 'extensions\\.worktreeconfig|', '', ['test_sandbox_integrity.SandboxIntegrityTests.test_worktree_config_enabled_by_the_worker_is_invalid_and_restored']),
    ('soso/engine.py', '"--includes", "--list"', '"--list"',
     ['test_evaluated_tree.EvaluatedTreeTests.test_include_activated_by_an_unwatched_setting_ends_the_candidate_and_blocks_new_runs']),
    ('soso/engine.py', 'guarded = self.config.get("allow_exec")', 'guarded = True',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_user_setting_during_a_run_without_exec_is_left_alone']),
    ('soso/engine.py', 'if restored:  # logged on every path', 'if False:  # logged on every path',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_restore_is_logged_when_the_candidate_ends_with_an_error']),
    ('soso/engine.py', 'if resume and saved.is_file():', 'if False:', ['test_sandbox_integrity.SandboxIntegrityTests.test_resumed_run_keeps_the_recorded_baseline']),
    ('soso/engine.py', 'drift = differs_from_commit(path, result["commit"], flagged)', 'drift = []',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_filtered_blob_left_in_the_stat_cache_is_invalid', 'test_sandbox_integrity.SandboxIntegrityTests.test_filter_given_on_the_command_line_is_invalid']),
    ('soso/engine.py', 'git(path, "update-index", "-q", "--refresh", env=env, ok=(0, 1))', 'None',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_crlf_file_edits_stay_valid_under_line_ending_settings']),
    ('soso/engine.py', 'if name and name not in skipped]', 'if name]',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_sparse_checkout_flags_inherited_by_worktrees_do_not_invalidate_candidates']),
    ('soso/engine.py', 'flagged = index_flags(path)', 'flagged = set()',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_sparse_checkout_flags_inherited_by_worktrees_do_not_invalidate_candidates']),
    ('soso/engine.py', 'x[:1] == "S" or x[:1].islower()', 'False',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_hiding_worktree_changes_with_index_flags_is_invalid']),
    ('soso/engine.py', 'git(path, "clean", "-ffdqx", ok=(0, 1), env={"GIT_INDEX_FILE": str(empty)})', 'None',
     ['test_delivery.DeliveryTests.test_candidate_ignored_shadow_files_do_not_raise_accepted_score']),
    ('soso/delivery.py', "git(root, 'clean', '-ffdqX')", 'None',
     ['test_delivery.DeliveryTests.test_baseline_ignores_files_left_by_earlier_evaluator_runs']),
    ('soso/engine.py', 'written = os.path.normpath(os.path.join(root, name))', 'written = os.path.join(root, name)',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_run_protects_normalised_program_paths_inside_repo_only']),
    ('soso/engine.py', 'for path in (written, os.path.realpath(written)):', 'for path in (written,):',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_program_paths_resolve_symlinks']),
    ('soso/engine.py', 'if os.path.commonpath([root, path]) == root and (not existing or os.path.isfile(path)):',
     'if not existing or os.path.isfile(path):', ['test_sandbox_integrity.SandboxIntegrityTests.test_run_protects_normalised_program_paths_inside_repo_only']),
    ('soso/engine.py', 'except (OSError, ValueError):\n            continue  # e.g. a -c program',
     'except ():\n            continue  # e.g. a -c program', ['test_sandbox_integrity.SandboxIntegrityTests.test_program_selection_tolerates_unusable_names']),
    ('soso/engine.py', 'if PYTHON.fullmatch(Path(arg).name) and (i == 0 or command[i - 1] not in VALUE_OPTIONS):', 'if i == 0:',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_python_m_is_recognised_only_for_python_in_its_spellings']),
    ('soso/engine.py', 'for i, arg in enumerate(command):', 'for i, arg in enumerate(command[:1]):',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_python_m_is_recognised_behind_a_wrapper']),
    ('soso/engine.py', '(python|pypy)', '(python)', ['test_sandbox_integrity.SandboxIntegrityTests.test_python_m_is_recognised_for_more_interpreters_and_runners']),
    ('soso/engine.py', '(\\.exe)?", re.I)', '(\\.exe)?")', ['test_sandbox_integrity.SandboxIntegrityTests.test_python_m_is_recognised_for_more_interpreters_and_runners']),
    ('soso/engine.py', ' and (i == 0 or command[i - 1] not in VALUE_OPTIONS)', '',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_python_m_is_recognised_for_more_interpreters_and_runners']),
    ('soso/engine.py', 'if Path(arg).name.casefold() in ("coverage", "uv") and command[i + 1] == "run":', 'if False:',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_python_m_is_recognised_for_more_interpreters_and_runners']),
    ('soso/engine.py', 'for j, flag in enumerate(arg[1:], 2):', 'for j, flag in enumerate(arg[1:2], 2):',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_python_m_is_recognised_only_for_python_in_its_spellings']),
    ('soso/engine.py', 'value = arg[j:] or (', 'value = (',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_python_m_is_recognised_only_for_python_in_its_spellings']),
    ('soso/engine.py', 'if kind == "-m" and all(parts):', 'if False:',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_run_protects_module_named_by_python_m']),
    ('soso/engine.py', 'names += [("/".join(parts[:i]) + "/__init__.py", False), ("/".join(parts[:i]) + ".py", False)]', 'pass',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_run_protects_parent_packages_of_python_m_module',
      'test_sandbox_integrity.SandboxIntegrityTests.test_python_m_module_names_are_protected_before_they_exist']),
    ('soso/engine.py', '(module + "/__main__.py", False)', '(module + "/__main__.py", True)',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_python_m_module_names_are_protected_before_they_exist']),
    ('soso/engine.py', 'elif kind == "path" and os.path.isdir(os.path.join(root, target)):', 'elif False:',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_python_directory_argument_protects_its_main']),
    ('soso/delivery.py', "protected_commands = local_programs(root, [cmd for cmd in [supplied_evaluate] + config.get('checks', []) if cmd])",
     'protected_commands = []', ['test_delivery.DeliveryTests.test_configured_python_m_evaluator_and_its_packages_are_protected']),
    ('soso/optimize.py', "programs = local_programs(project, [config['evaluate']] + config.get('checks', []))", 'programs = []',
     ['test_optimize_launch.PreparedOptimizationTests.test_python_m_evaluator_and_its_packages_are_frozen_as_protected']),
    ('soso/launch.py', 'if not isinstance(entry, dict):', 'if False:',
     ['test_launch.LauncherTests.test_malformed_saved_entry_does_not_block_resume']),
    ('soso/launch.py', 'isinstance(fingerprint, str) and ', '',
     ['test_launch.LauncherTests.test_malformed_saved_entry_does_not_block_resume']),
    ('soso/launch.py', 'if not isinstance(saved, dict):', 'if False:',
     ['test_launch.LauncherTests.test_malformed_active_record_is_a_clear_resume_error']),
    ('soso/launch.py', 'if not isinstance(task_id, str) or not task_id', 'if not task_id',
     ['test_launch.LauncherTests.test_malformed_active_record_is_a_clear_resume_error']),
    ('soso/intent.py', 'routes = [names for names in ([n for n in preferred if n not in same], same) if names] if same else [preferred]',
     'routes = [preferred]', ['test_review_intent.IntentCompilerReviewTests.test_policy_review_runs_on_a_model_other_than_the_compiler']),
    ('soso/intent.py', 'compiler is not None and identity(n) == identity(compiler)', 'compiler is not None and n == compiler',
     ['test_review_intent.IntentCompilerReviewTests.test_review_skips_aliases_of_the_compiler_when_ordering_reviewers']),
    ('soso/intent.py', 'if names is routes[-1]:', 'if True:', ['test_review_intent.IntentCompilerReviewTests.test_off_protocol_independent_reviewer_falls_back_to_compiler']),
    ('soso/intent.py', 'temporary.unlink(missing_ok=True)  # never leave an unreviewed policy behind', 'pass',
     ['test_review_intent.IntentCompilerReviewTests.test_failed_policy_review_leaves_no_pending_policy']),
    ('soso/intent.py', 'identity(reviewer) != identity(compiler)', 'reviewer != compiler',
     ['test_review_intent.IntentCompilerReviewTests.test_alias_of_the_compiler_is_not_an_independent_reviewer']),
    ('soso/intent.py', 'independent=None not in (compiler, reviewer) and ', 'independent=',
     ['test_review_intent.IntentCompilerReviewTests.test_missing_explicit_classifier_fallback_is_rejected_then_repaired']),
    ('soso/process.py', '"-c", "core.fsmonitor=false", ', '',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_runner_git_ignores_a_preconfigured_fsmonitor_hook']),
    ('soso/process.py', 'os.killpg(proc.pid, signal.SIGKILL)', 'proc.kill()',
     ['test_core.ProcessTests.test_descendant_cleanup']),
    ('soso/engine.py', 'git(path, "clean", "-ffdqx", ok=(0, 1), env={"GIT_INDEX_FILE": str(empty)})', 'None',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_candidate_is_evaluated_without_ignored_files']),
    ('soso/engine.py', ' and "review" not in x]', ']',
     ['test_run_reliability.IncompleteRoundTests.test_rejected_candidate_is_not_reviewed_again_on_resume']),
    ('soso/engine.py', 'raise ValueError("worker changed Git history")', 'pass',
     ['test_guards.GuardTests.test_worker_cannot_commit_candidate_history']),
    ('soso/engine.py', 'raise ValueError("protected files cannot be symlinks")', 'pass',
     ['test_guards.GuardTests.test_protected_file_replaced_by_identical_symlink_is_rejected']),
    ('soso/engine.py', 'raise ValueError("result HEAD changed since checkpoint; inspect before resuming")', 'pass',
     ['test_guards.GuardTests.test_resume_rejects_result_head_moved_since_checkpoint']),
    ('soso/cli.py', 'config["protected"] += [name for name in dict.fromkeys(programs) if name not in config["protected"]]',
     'pass', ['test_sandbox_integrity.SandboxIntegrityTests.test_run_protects_in_repo_evaluator_and_check_programs']),
    ('soso/cli.py', 'if name not in config["protected"]]', ']',
     ['test_sandbox_integrity.SandboxIntegrityTests.test_already_protected_evaluator_leaves_config_unchanged']),
    ('soso/delivery.py', "raise ValueError('goal acceptance policy changed')", 'pass',
     ['test_delivery.DeliveryTests.test_policy_tampering_with_recorded_head_is_blocked']),
    # The run_epoch check; repair_candidate's copy is indented less.
    ('soso/delivery.py', "                        raise ValueError('review modified accepted source')",
     '                        pass', ['test_delivery.DeliveryTests.test_approving_review_that_leaves_files_is_not_integrated']),
    ('soso/delivery.py', "raise ValueError('prepared acceptance evaluator changed')", 'pass',
     ['test_delivery.DeliveryTests.test_prepared_contract_evaluator_is_verified_before_recovery']),
    ('soso/dashboard.py', "raise ValueError('unknown asset')", 'pass',
     ['test_dashboard.DashboardTests.test_http_requires_token_and_serves_no_arbitrary_files']),
    ('soso/models.py', 'for form in (json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1], value):',
     'for form in (value,):',
     ['test_delivery.DeliveryTests.test_prompt_redacts_secret_escaped_by_embedded_json']),
    ('soso/delivery.py', "json.dumps(redact_value({'time': time.time(), 'event': event, **data}))",
     "json.dumps({'time': time.time(), 'event': event, **data})",
     ['test_delivery.DeliveryTests.test_setup_events_redact_environment_secrets']),
    ('soso/intent.py', "json.dumps(redact_value({'time': time.time(), 'event': event, **data}))",
     "json.dumps({'time': time.time(), 'event': event, **data})",
     ['test_intent.MarkdownPoolTests.test_setup_events_redact_environment_secrets']),
    ('soso/delivery.py', "self.report.update(status='blocked', reason=str(exc)[:2000])", 'pass',
     ['test_delivery.DeliveryTests.test_terminal_planning_or_review_error_is_recorded_in_epoch_report']),
    ('soso/process.py', 'common_dir(root) in IDENTITY["scope"]', 'True',
     ['test_core.IdentityTests.test_fallback_identity_stays_in_the_repository_it_was_derived_for']),
    ('soso/delivery.py', "IDENTITY['scope'] = {*IDENTITY.get('scope', ()), common_dir(repo)}", 'pass',
     ['test_core.IdentityTests.test_fallback_identity_stays_in_the_repository_it_was_derived_for']),
    # Not a guard but a pin: anything start_goal adds to the epoch config must change the draft binding digest.
    ('soso/delivery.py', "config['direction'] = 'max'", "config['direction'] = 'max'; config['added'] = True",
     ['test_resume_digest.DraftBindingDigestTests.test_delivery_draft_binding_digest_is_pinned']),
    ('soso/dashboard.py', 'p.is_symlink() for p in [candidate, *candidate.parents]', 'False for p in [candidate]',
     ['test_dashboard.DashboardTests.test_listed_image_replaced_by_symlink_is_not_served_from_cache']),
    ('soso/models.py', 'json.dumps(value, ensure_ascii=False)[1:-1], ', '',
     ['test_run_reliability.RedactionTests.test_secret_in_text_built_without_ascii_escaping_is_redacted']),
    # The accepted commit is what was evaluated: a fresh checkout of it, reviewed as stored.
    ('soso/engine.py', 'elif checkout(path, result["commit"], flagged):', 'elif False:',
     ['test_evaluated_tree.EvaluatedTreeTests.test_ident_attribute_cannot_hide_the_scored_code', 'test_evaluated_tree.EvaluatedTreeTests.test_empty_directory_is_not_scored',
      'test_evaluated_tree.EvaluatedTreeTests.test_executable_bit_hidden_under_the_users_file_mode_off_is_not_scored',
      'test_evaluated_tree.EvaluatedTreeTests.test_file_outside_the_sparse_checkout_is_not_scored_under_the_users_setting']),
    ('soso/engine.py', 'git(path, "update-index", "-z", "--skip-worktree", "--stdin", stdin=skipped)', 'None',
     ['test_evaluated_tree.EvaluatedTreeTests.test_sparse_cone_with_sparse_index_stays_valid']),
    ('soso/engine.py', 'left = [name for name in os.listdir(path) if name != ".git"]', 'left = []',
     ['test_evaluated_tree.EvaluatedTreeTests.test_special_file_is_not_scored']),
    ('soso/engine.py', 'elif guarded and self.unsettled(path, own, restored, restores):', 'elif False:',
     ['test_evaluated_tree.EvaluatedTreeTests.test_config_change_during_checkout_is_invalid']),
    ('soso/engine.py', 'self.restores += bool(changed)', 'pass', ['test_evaluated_tree.EvaluatedTreeTests.test_config_change_during_checkout_is_invalid']),
    ('soso/engine.py', 'elif fingerprint(path, self.config["protected"]) != self.pinned:', 'elif False:',
     ['test_evaluated_tree.EvaluatedTreeTests.test_attributes_that_change_a_protected_file_at_checkout_are_invalid']),
    ('soso/engine.py', 'elif git(path, "status", "--porcelain"):  # e.g. paths', 'elif False:  # e.g. paths',
     ['test_evaluated_tree.EvaluatedTreeTests.test_files_that_differ_from_the_commit_after_checkout_are_invalid']),
    ('soso/engine.py', 'elif "120000" in modes and', 'elif False and',
     ['test_evaluated_tree.EvaluatedTreeTests.test_symlink_staged_under_the_users_symlinks_off_is_not_scored']),
    ('soso/engine.py', 'symlinks|filemode', 'filemode', ['test_evaluated_tree.EvaluatedTreeTests.test_symlink_staged_with_symlinks_turned_off_is_not_scored']),
    ('soso/engine.py', '(symlinks|filemode)', '(symlinks)', ['test_evaluated_tree.EvaluatedTreeTests.test_executable_bit_hidden_with_file_mode_turned_off_is_not_scored']),
    ('soso/engine.py', '|sparse\\..*"', '"', ['test_evaluated_tree.EvaluatedTreeTests.test_file_outside_the_sparse_checkout_is_not_scored']),
    ('soso/engine.py', 'files = included(path, self.config_file) if names is None else', 'files = {} if names is None else',
     ['test_evaluated_tree.EvaluatedTreeTests.test_filter_planted_in_an_included_file_is_restored_and_not_adopted_by_the_next_run']),
    ('soso/engine.py', '                        stream.write(data)', '                        pass',
     ['test_evaluated_tree.EvaluatedTreeTests.test_filter_planted_in_an_included_file_is_restored_and_not_adopted_by_the_next_run',
      'test_sandbox_integrity.SandboxIntegrityTests.test_change_inside_an_existing_included_file_is_not_accepted']),
    ('soso/engine.py', '                    os.unlink(name)', '                    pass', ['test_evaluated_tree.EvaluatedTreeTests.test_included_file_the_worker_creates_is_removed']),
    ('soso/engine.py', ' | os.O_NOFOLLOW', '', ['test_evaluated_tree.EvaluatedTreeTests.test_included_file_replaced_by_a_symlink_is_not_written_through']),
    ('soso/engine.py', 'problems.append((name, f"could not restore {name}; see config_restored"))', 'pass',
     ['test_evaluated_tree.EvaluatedTreeTests.test_included_file_replaced_by_a_symlink_is_not_written_through']),
    ('soso/engine.py', 'atomic_json(self.unrestored, {"run": self.run_id, "file": ", ".join(file for file, _ in problems)})', 'None',
     ['test_evaluated_tree.EvaluatedTreeTests.test_run_refuses_to_start_after_a_failed_restore_until_it_is_repaired']),
    ('soso/engine.py', 'if self.shared(self.root, recorded[2]) != recorded:', 'if False:',
     ['test_evaluated_tree.EvaluatedTreeTests.test_run_refuses_to_start_after_a_failed_restore_until_it_is_repaired',
      'test_evaluated_tree.EvaluatedTreeTests.test_include_activated_by_an_unwatched_setting_ends_the_candidate_and_blocks_new_runs']),
    ('soso/engine.py', '            self.unrestored.unlink()', '            pass',
     ['test_evaluated_tree.EvaluatedTreeTests.test_run_refuses_to_start_after_a_failed_restore_until_it_is_repaired']),
    ('soso/process.py', 'GIT_ADVICE="0", ', '', ['test_evaluated_tree.EvaluatedTreeTests.test_sparse_cone_with_sparse_index_stays_valid']),
    ('soso/process.py', 'GIT_NO_REPLACE_OBJECTS="1", ', '', ['test_evaluated_tree.EvaluatedTreeTests.test_replace_objects_do_not_hide_the_change_from_the_reviewer']),
    ('soso/engine.py', '"diff", "--no-ext-diff", ', '"diff", ', ['test_evaluated_tree.EvaluatedTreeTests.test_external_diff_does_not_hide_the_change_from_the_reviewer']),
    ('soso/engine.py', '"--no-textconv", "HEAD"', '"HEAD"', ['test_evaluated_tree.EvaluatedTreeTests.test_textconv_does_not_hide_the_change_from_the_reviewer']),
    ('soso/delivery.py', "'--no-ext-diff', '--no-textconv', base", "'--no-textconv', base",
     ['test_evaluated_tree.EvaluatedTreeTests.test_delivery_reviewer_diff_ignores_external_diff_and_textconv']),
    ('soso/delivery.py', "'--no-textconv', base", "base", ['test_evaluated_tree.EvaluatedTreeTests.test_delivery_reviewer_diff_ignores_external_diff_and_textconv']),
]


# An inherited GIT_DIR or GIT_WORK_TREE would point the snapshot's Git commands at a real repository.
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')}, 'PYTHONDONTWRITEBYTECODE': '1'}


def tests_fail(root, tests):
    env = {**ENV, 'PYTHONPATH': os.pathsep.join([str(root / 'tests'), str(root)])}
    result = subprocess.run([sys.executable, '-m', 'unittest', *tests], cwd=root, env=env,
                            capture_output=True, text=True, timeout=900)
    return result.returncode != 0, result.stderr[-3000:]


def snapshot(source, root):
    shutil.copytree(source, root, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '.claude', '.so-so', '__pycache__', '.venv'))
    for argv in (['init', '-q', '-b', 'main'], ['add', '-A'],
                 ['-c', 'user.name=check', '-c', 'user.email=check@example.invalid', '-c', 'commit.gpgsign=false',
                  '-c', 'core.hooksPath=/dev/null', 'commit', '-q', '-m', 'snapshot']):
        subprocess.run(['git', *argv], cwd=root, env=ENV, check=True, capture_output=True)


def main():
    problems = []
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / 'repo'
        snapshot(Path(__file__).resolve().parent.parent, root)
        failed, log = tests_fail(root, sorted({t for *_, tests in GUARDS for t in tests}))
        if failed:
            print('check_guards: guard tests fail with every guard in place\n' + log, file=sys.stderr)
            return 1
        for file, guard, disabled, tests in GUARDS:
            path = root / file
            original = path.read_text()
            if original.count(guard) != 1:
                problems.append(f'{file}: guard text must occur exactly once: {guard}')
                continue
            mutant = original.replace(guard, disabled)
            try:
                compile(mutant, file, 'exec')  # a syntax error would fail the tests for the wrong reason
            except SyntaxError as exc:
                problems.append(f'{file}: disabled guard does not compile ({exc.msg}): {guard.strip()}')
                continue
            path.write_text(mutant)
            try:
                failed, _ = tests_fail(root, tests)
            except subprocess.TimeoutExpired:
                problems.append(f'{file}: tests timed out with this guard disabled: {guard.strip()}')
                continue
            finally:
                path.write_text(original)
            print(('red   ' if failed else 'GREEN ') + file + ': ' + guard.strip(), flush=True)
            if not failed:
                problems.append(f'{file}: tests still pass with this guard disabled: {guard.strip()}')
    for problem in problems:
        print('check_guards: ' + problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())
