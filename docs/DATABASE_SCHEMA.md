# Database schema

25 tables (SQLite, managed by Alembic).

## `agent_definitions`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| key | VARCHAR(64) | no |  |
| name | VARCHAR(128) | no |  |
| category | VARCHAR(24) | no |  |
| description | TEXT | no |  |
| enabled | BOOLEAN | no |  |
| permissions | JSON | no |  |
| config | JSON | no |  |
| created_at | DATETIME | no |  |
| updated_at | DATETIME | no |  |

## `agent_executions`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| task_id | INTEGER | yes | FK → tasks.id |
| agent_id | INTEGER | yes | FK → agent_definitions.id |
| agent_key | VARCHAR(64) | no |  |
| step_key | VARCHAR(64) | no |  |
| attempt | INTEGER | no |  |
| status | VARCHAR(24) | no |  |
| input | JSON | no |  |
| output | JSON | no |  |
| error | TEXT | yes |  |
| started_at | DATETIME | no |  |
| finished_at | DATETIME | yes |  |
| duration_ms | INTEGER | yes |  |

Indexes: `ix_agent_executions_agent_id` (agent_id); `ix_agent_executions_task_id` (task_id)

## `ai_providers`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| key | VARCHAR(64) | no |  |
| display_name | VARCHAR(128) | no |  |
| kind | VARCHAR(32) | no |  |
| capabilities | JSON | no |  |
| enabled | BOOLEAN | no |  |
| external_consent | BOOLEAN | no |  |
| config | JSON | no |  |
| created_at | DATETIME | no |  |
| updated_at | DATETIME | no |  |

## `approvals`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| task_id | INTEGER | yes | FK → tasks.id |
| action | VARCHAR(48) | no |  |
| summary | TEXT | no |  |
| detail | JSON | no |  |
| status | VARCHAR(16) | no |  |
| requested_by | VARCHAR(64) | no |  |
| requested_at | DATETIME | no |  |
| decided_at | DATETIME | yes |  |
| decision_note | TEXT | no |  |

Indexes: `ix_approvals_task_id` (task_id)

## `audit_logs`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| ts | DATETIME | no |  |
| actor | VARCHAR(96) | no |  |
| action | VARCHAR(96) | no |  |
| resource_type | VARCHAR(48) | no |  |
| resource_id | VARCHAR(255) | no |  |
| outcome | VARCHAR(16) | no |  |
| permission_level | VARCHAR(32) | no |  |
| detail | JSON | no |  |
| prev_hash | VARCHAR(64) | no |  |
| hash | VARCHAR(64) | no |  |

Indexes: `ix_audit_logs_action` (action); `ix_audit_logs_ts` (ts)

## `captured_responses`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| session_id | INTEGER | no | FK → dev_sessions.id |
| prompt_id | INTEGER | yes | FK → user_prompts.id |
| sequence | INTEGER | no |  |
| role | VARCHAR(16) | no |  |
| model | VARCHAR(128) | yes |  |
| content | TEXT | no |  |
| tool_calls | JSON | no |  |
| injection_flags | JSON | no |  |
| redactions | INTEGER | no |  |
| created_at | DATETIME | no |  |

Indexes: `ix_captured_responses_session_seq` (session_id, sequence)

## `code_implementations`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| project_id | INTEGER | yes | FK → projects.id |
| pattern_id | INTEGER | yes | FK → knowledge_patterns.id |
| session_id | INTEGER | yes | FK → dev_sessions.id |
| task_id | INTEGER | yes | FK → tasks.id |
| kind | VARCHAR(16) | no |  |
| request | TEXT | no |  |
| language | VARCHAR(32) | yes |  |
| model | VARCHAR(128) | yes |  |
| files | JSON | no |  |
| status | VARCHAR(16) | no |  |
| verified | BOOLEAN | no |  |
| created_at | DATETIME | no |  |

Indexes: `ix_code_implementations_task_id` (task_id); `ix_code_implementations_project_id` (project_id); `ix_code_implementations_pattern_id` (pattern_id)

## `debugging_solutions`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| error_id | INTEGER | yes | FK → errors.id |
| signature | VARCHAR(64) | no |  |
| root_cause | TEXT | no |  |
| description | TEXT | no |  |
| fix | JSON | no |  |
| verified | BOOLEAN | no |  |
| verified_by_testing_session_id | INTEGER | yes | FK → testing_sessions.id |
| use_count | INTEGER | no |  |
| created_at | DATETIME | no |  |

Indexes: `ix_debugging_solutions_signature` (signature)

## `dependencies`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| project_id | INTEGER | yes | FK → projects.id |
| pattern_id | INTEGER | yes | FK → knowledge_patterns.id |
| ecosystem | VARCHAR(16) | no |  |
| name | VARCHAR(255) | no |  |
| version_spec | VARCHAR(128) | no |  |
| is_dev | BOOLEAN | no |  |
| source_file | VARCHAR(1024) | yes |  |
| created_at | DATETIME | no |  |

Indexes: `ix_dependencies_project_name` (project_id, ecosystem, name)

## `dev_sessions`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| project_id | INTEGER | no | FK → projects.id |
| provider_id | INTEGER | yes | FK → ai_providers.id |
| external_id | VARCHAR(255) | yes |  |
| title | VARCHAR(512) | no |  |
| status | VARCHAR(24) | no |  |
| outcome | VARCHAR(24) | no |  |
| capture_source | VARCHAR(24) | no |  |
| started_at | DATETIME | no |  |
| ended_at | DATETIME | yes |  |
| git_head_start | VARCHAR(64) | yes |  |
| git_head_end | VARCHAR(64) | yes |  |
| summary | TEXT | no |  |
| meta | JSON | no |  |
| created_at | DATETIME | no |  |
| updated_at | DATETIME | no |  |

Indexes: `ix_dev_sessions_project_started` (project_id, started_at)

## `embedding_index`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| pattern_id | INTEGER | no | FK → knowledge_patterns.id |
| version_id | INTEGER | no |  |
| model | VARCHAR(128) | no |  |
| dim | INTEGER | no |  |
| indexed_at | DATETIME | no |  |

## `errors`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| project_id | INTEGER | yes | FK → projects.id |
| session_id | INTEGER | yes | FK → dev_sessions.id |
| task_id | INTEGER | yes | FK → tasks.id |
| source | VARCHAR(16) | no |  |
| message | TEXT | no |  |
| stack | TEXT | no |  |
| signature | VARCHAR(64) | no |  |
| file_path | VARCHAR(2048) | yes |  |
| line | INTEGER | yes |  |
| resolved | BOOLEAN | no |  |
| created_at | DATETIME | no |  |

Indexes: `ix_errors_project_id` (project_id); `ix_errors_signature` (signature)

## `file_changes`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| session_id | INTEGER | no | FK → dev_sessions.id |
| path | VARCHAR(2048) | no |  |
| change_type | VARCHAR(16) | no |  |
| old_path | VARCHAR(2048) | yes |  |
| before_snapshot_id | INTEGER | yes | FK → file_snapshots.id |
| after_snapshot_id | INTEGER | yes | FK → file_snapshots.id |
| diff | TEXT | no |  |
| lines_added | INTEGER | no |  |
| lines_removed | INTEGER | no |  |
| source | VARCHAR(24) | no |  |
| created_at | DATETIME | no |  |

Indexes: `ix_file_changes_session_path` (session_id, path)

## `file_snapshots`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| project_id | INTEGER | no | FK → projects.id |
| session_id | INTEGER | yes | FK → dev_sessions.id |
| path | VARCHAR(2048) | no |  |
| content_hash | VARCHAR(64) | no |  |
| size | INTEGER | no |  |
| language | VARCHAR(32) | yes |  |
| kind | VARCHAR(16) | no |  |
| redactions | INTEGER | no |  |
| created_at | DATETIME | no |  |

Indexes: `ix_file_snapshots_project_path` (project_id, path); `ix_file_snapshots_content_hash` (content_hash)

## `knowledge_patterns`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| uid | VARCHAR(32) | no |  |
| title | VARCHAR(512) | no |  |
| summary | TEXT | no |  |
| category | VARCHAR(64) | no |  |
| tags | JSON | no |  |
| language | VARCHAR(32) | yes |  |
| framework | VARCHAR(64) | yes |  |
| framework_version | VARCHAR(64) | yes |  |
| status | VARCHAR(16) | no |  |
| confidence | FLOAT | no |  |
| fingerprint | VARCHAR(64) | no |  |
| project_id | INTEGER | yes | FK → projects.id |
| origin_session_id | INTEGER | yes | FK → dev_sessions.id |
| provenance | JSON | no |  |
| use_count | INTEGER | no |  |
| success_count | INTEGER | no |  |
| failure_count | INTEGER | no |  |
| last_used_at | DATETIME | yes |  |
| verified_at | DATETIME | yes |  |
| verified_by_testing_session_id | INTEGER | yes |  |
| current_version_id | INTEGER | yes | FK → pattern_versions.id |
| created_at | DATETIME | no |  |
| updated_at | DATETIME | no |  |

Indexes: `ix_knowledge_patterns_lang_fw` (language, framework); `ix_knowledge_patterns_status` (status); `ix_knowledge_patterns_fingerprint` (fingerprint); `ix_knowledge_patterns_category` (category); `ix_knowledge_patterns_verified_by_testing_session_id` (verified_by_testing_session_id)

## `pattern_relationships`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| source_id | INTEGER | no | FK → knowledge_patterns.id |
| target_id | INTEGER | no | FK → knowledge_patterns.id |
| relation | VARCHAR(24) | no |  |
| score | FLOAT | no |  |
| note | TEXT | no |  |
| created_at | DATETIME | no |  |

Indexes: `ix_pattern_relationships_source_id` (source_id); `ix_pattern_relationships_target_id` (target_id)

## `pattern_versions`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| pattern_id | INTEGER | no | FK → knowledge_patterns.id |
| version_number | INTEGER | no |  |
| title | VARCHAR(512) | no |  |
| description | TEXT | no |  |
| content | JSON | no |  |
| content_hash | VARCHAR(64) | no |  |
| change_note | TEXT | no |  |
| created_by | VARCHAR(32) | no |  |
| parent_version_id | INTEGER | yes | FK → pattern_versions.id |
| created_at | DATETIME | no |  |

Indexes: `ix_pattern_versions_pattern_id` (pattern_id)

## `projects`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| name | VARCHAR(255) | no |  |
| root_path | VARCHAR(2048) | no |  |
| languages | JSON | no |  |
| frameworks | JSON | no |  |
| conventions | JSON | no |  |
| git_remote | VARCHAR(2048) | yes |  |
| capture_enabled | BOOLEAN | no |  |
| share_knowledge | BOOLEAN | no |  |
| created_at | DATETIME | no |  |
| updated_at | DATETIME | no |  |

## `recovery_points`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| project_id | INTEGER | no | FK → projects.id |
| task_id | INTEGER | yes | FK → tasks.id |
| label | VARCHAR(255) | no |  |
| files | JSON | no |  |
| status | VARCHAR(16) | no |  |
| created_at | DATETIME | no |  |
| restored_at | DATETIME | yes |  |

Indexes: `ix_recovery_points_project_id` (project_id)

## `regression_baselines`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| project_id | INTEGER | no | FK → projects.id |
| test_key | VARCHAR(1024) | no |  |
| status | VARCHAR(16) | no |  |
| updated_at | DATETIME | no |  |

Indexes: `ix_regression_baselines_project_id` (project_id)

## `tasks`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| project_id | INTEGER | yes | FK → projects.id |
| kind | VARCHAR(24) | no |  |
| request | TEXT | no |  |
| status | VARCHAR(24) | no |  |
| mode | VARCHAR(16) | no |  |
| permission_level | VARCHAR(32) | no |  |
| plan | JSON | no |  |
| result | JSON | no |  |
| error | TEXT | yes |  |
| verified | BOOLEAN | no |  |
| created_at | DATETIME | no |  |
| started_at | DATETIME | yes |  |
| finished_at | DATETIME | yes |  |

Indexes: `ix_tasks_project_created` (project_id, created_at)

## `test_results`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| testing_session_id | INTEGER | no | FK → testing_sessions.id |
| agent_key | VARCHAR(64) | no |  |
| name | VARCHAR(1024) | no |  |
| description | TEXT | no |  |
| expected | TEXT | no |  |
| actual | TEXT | no |  |
| status | VARCHAR(16) | no |  |
| error_details | TEXT | no |  |
| correction_applied | TEXT | no |  |
| retest_status | VARCHAR(16) | yes |  |
| duration_ms | INTEGER | yes |  |
| file_path | VARCHAR(2048) | yes |  |
| created_at | DATETIME | no |  |

Indexes: `ix_test_results_testing_session_id` (testing_session_id)

## `testing_sessions`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| project_id | INTEGER | yes | FK → projects.id |
| task_id | INTEGER | yes | FK → tasks.id |
| pattern_id | INTEGER | yes | FK → knowledge_patterns.id |
| agent_key | VARCHAR(64) | no |  |
| status | VARCHAR(16) | no |  |
| command | TEXT | no |  |
| summary | JSON | no |  |
| coverage_percent | FLOAT | yes |  |
| started_at | DATETIME | no |  |
| finished_at | DATETIME | yes |  |

Indexes: `ix_testing_sessions_task_id` (task_id); `ix_testing_sessions_project_started` (project_id, started_at)

## `user_feedback`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| target_type | VARCHAR(24) | no |  |
| target_id | INTEGER | no |  |
| action | VARCHAR(16) | no |  |
| rating | INTEGER | yes |  |
| comment | TEXT | no |  |
| correction | JSON | no |  |
| created_at | DATETIME | no |  |

Indexes: `ix_user_feedback_target` (target_type, target_id)

## `user_prompts`

| Column | Type | Null | Key |
|---|---|---|---|
| id | INTEGER | no | PK |
| session_id | INTEGER | no | FK → dev_sessions.id |
| sequence | INTEGER | no |  |
| content | TEXT | no |  |
| content_hash | VARCHAR(64) | no |  |
| redactions | INTEGER | no |  |
| created_at | DATETIME | no |  |

Indexes: `ix_user_prompts_session_seq` (session_id, sequence)
