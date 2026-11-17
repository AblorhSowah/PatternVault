# PatternVault API reference

Version 0.1.0. All routes except `/health` require `Authorization: Bearer <token>`.

## `GET /api/v1/agents`
List Agents
Responses: `200`

## `PATCH /api/v1/agents/{key}`
Update Agent
Parameters: `key` (path, required)
Request body: `AgentUpdate`
Responses: `200`, `422`

## `GET /api/v1/approvals`
Approvals
Parameters: `pending_only` (query)
Responses: `200`, `422`

## `POST /api/v1/approvals/{approval_id}/decide`
Decide
Parameters: `approval_id` (path, required)
Request body: `ApprovalDecision`
Responses: `200`, `422`

## `GET /api/v1/audit`
List Audit
Parameters: `limit` (query), `action` (query), `before_id` (query)
Responses: `200`, `422`

## `GET /api/v1/audit/verify`
Audit Verify
Responses: `200`

## `GET /api/v1/backups`
List Backups
Responses: `200`

## `POST /api/v1/backups`
Create Backup
Request body: `BackupCreate`
Responses: `201`, `422`

## `POST /api/v1/backups/restore`
Restore Backup
Request body: `BackupRestore`
Responses: `200`, `422`

## `POST /api/v1/backups/verify`
Verify Backup
Request body: `BackupRestore`
Responses: `200`, `422`

## `DELETE /api/v1/backups/{name}`
Delete Backup
Parameters: `name` (path, required), `confirm` (query)
Responses: `204`, `422`

## `DELETE /api/v1/chat/conversations`
Delete All Conversations
Responses: `200`

## `GET /api/v1/chat/conversations`
List Conversations
Parameters: `limit` (query)
Responses: `200`, `422`

## `POST /api/v1/chat/conversations`
Create Conversation
Request body: `ConversationCreate`
Responses: `201`, `422`

## `DELETE /api/v1/chat/conversations/{conversation_id}`
Delete Conversation
Parameters: `conversation_id` (path, required)
Responses: `200`, `422`

## `GET /api/v1/chat/conversations/{conversation_id}`
Get Conversation
Parameters: `conversation_id` (path, required)
Responses: `200`, `422`

## `PATCH /api/v1/chat/conversations/{conversation_id}`
Update Conversation
Parameters: `conversation_id` (path, required)
Request body: `ConversationUpdate`
Responses: `200`, `422`

## `POST /api/v1/chat/conversations/{conversation_id}/messages`
Send Message
Parameters: `conversation_id` (path, required), `x-pv-cloud-key` (header)
Request body: `ChatMessageIn`
Responses: `202`, `422`

## `GET /api/v1/chat/turns/{turn_id}`
Get Turn
Parameters: `turn_id` (path, required)
Responses: `200`, `422`

## `POST /api/v1/chat/turns/{turn_id}/cancel`
Cancel Turn
Parameters: `turn_id` (path, required)
Responses: `200`, `422`

## `POST /api/v1/compare`
Compare
Request body: `CompareIn`
Responses: `200`, `422`

## `GET /api/v1/config`
Get Config
Responses: `200`

## `PATCH /api/v1/config`
Patch Config
Request body: `JSON`
Responses: `200`, `422`

## `POST /api/v1/config/reset`
Reset Config
Responses: `200`

## `POST /api/v1/context`
Context
Request body: `ContextRequest`
Responses: `200`, `422`

## `GET /api/v1/dashboard`
Dashboard
Responses: `200`

## `POST /api/v1/generate`
Generate
Parameters: `x-pv-cloud-key` (header)
Request body: `GenerateIn`
Responses: `200`, `422`

## `GET /api/v1/health`
Health
Responses: `200`

## `GET /api/v1/implementations`
List Implementations
Parameters: `limit` (query)
Responses: `200`, `422`

## `GET /api/v1/implementations/{impl_id}`
Get Implementation
Parameters: `impl_id` (path, required)
Responses: `200`, `422`

## `GET /api/v1/knowledge/export`
Export
Parameters: `status` (query)
Responses: `200`, `422`

## `POST /api/v1/knowledge/import`
Import 
Request body: `JSON`
Responses: `200`, `422`

## `POST /api/v1/knowledge/quality`
Quality
Parameters: `apply` (query)
Responses: `200`, `422`

## `POST /api/v1/knowledge/reindex`
Reindex
Responses: `200`

## `GET /api/v1/knowledge/stats`
Stats
Responses: `200`

## `GET /api/v1/mode`
Get Mode
Responses: `200`

## `PUT /api/v1/mode`
Set Mode
Request body: `ModeRequest`
Responses: `200`, `422`

## `GET /api/v1/models`
Models
Responses: `200`

## `POST /api/v1/models/pull`
Pull
Request body: `ModelPull`
Responses: `202`, `422`

## `POST /api/v1/models/select`
Select
Request body: `ModelSelect`
Responses: `200`, `422`

## `GET /api/v1/models/status`
Status
Responses: `200`

## `POST /api/v1/models/test`
Test Model
Parameters: `model` (query)
Responses: `200`, `422`

## `GET /api/v1/modes/availability`
Availability
Responses: `200`

## `GET /api/v1/patterns`
List Patterns
Parameters: `status` (query), `category` (query), `language` (query), `framework` (query), `project_id` (query), `q` (query), `limit` (query), `offset` (query)
Responses: `200`, `422`

## `GET /api/v1/patterns/facets`
Facets
Responses: `200`

## `DELETE /api/v1/patterns/{pattern_id}`
Delete
Parameters: `pattern_id` (path, required), `confirm` (query)
Responses: `204`, `422`

## `GET /api/v1/patterns/{pattern_id}`
Get Pattern
Parameters: `pattern_id` (path, required)
Responses: `200`, `422`

## `PATCH /api/v1/patterns/{pattern_id}`
Update
Parameters: `pattern_id` (path, required)
Request body: `PatternUpdate`
Responses: `200`, `422`

## `POST /api/v1/patterns/{pattern_id}/approve`
Approve
Parameters: `pattern_id` (path, required), `comment` (query)
Responses: `200`, `422`

## `POST /api/v1/patterns/{pattern_id}/deprecate`
Deprecate
Parameters: `pattern_id` (path, required), `comment` (query)
Responses: `200`, `422`

## `POST /api/v1/patterns/{pattern_id}/feedback`
Feedback
Parameters: `pattern_id` (path, required)
Request body: `FeedbackIn`
Responses: `200`, `422`

## `POST /api/v1/patterns/{pattern_id}/relations`
Relate
Parameters: `pattern_id` (path, required)
Request body: `RelateIn`
Responses: `201`, `422`

## `POST /api/v1/patterns/{pattern_id}/restore`
Restore
Parameters: `pattern_id` (path, required)
Responses: `200`, `422`

## `GET /api/v1/patterns/{pattern_id}/versions`
Versions
Parameters: `pattern_id` (path, required)
Responses: `200`, `422`

## `GET /api/v1/projects`
List Projects
Responses: `200`

## `POST /api/v1/projects`
Register Project
Request body: `ProjectCreate`
Responses: `201`, `422`

## `DELETE /api/v1/projects/{project_id}`
Delete Project
Parameters: `project_id` (path, required), `confirm` (query)
Responses: `204`, `422`

## `GET /api/v1/projects/{project_id}`
Get Project
Parameters: `project_id` (path, required)
Responses: `200`, `422`

## `PATCH /api/v1/projects/{project_id}`
Update Project
Parameters: `project_id` (path, required)
Request body: `ProjectUpdate`
Responses: `200`, `422`

## `POST /api/v1/projects/{project_id}/analyze`
Analyze
Parameters: `project_id` (path, required)
Responses: `200`, `422`

## `GET /api/v1/providers`
List Providers
Responses: `200`

## `POST /api/v1/providers/{key}/import`
Import Session
Parameters: `key` (path, required)
Request body: `ImportRequest`
Responses: `201`, `422`

## `GET /api/v1/providers/{key}/sessions`
Provider Sessions
Parameters: `key` (path, required), `project_id` (query)
Responses: `200`, `422`

## `GET /api/v1/recovery-points`
Recovery Points
Parameters: `project_id` (query)
Responses: `200`, `422`

## `POST /api/v1/recovery-points/{point_id}/restore`
Restore
Parameters: `point_id` (path, required), `confirm` (query)
Responses: `200`, `422`

## `POST /api/v1/replay/plan`
Replay Plan
Request body: `ReplayIn`
Responses: `200`, `422`

## `POST /api/v1/search`
Search
Request body: `SearchRequest`
Responses: `200`, `422`

## `GET /api/v1/sessions`
List Sessions
Parameters: `project_id` (query), `status` (query), `limit` (query), `offset` (query)
Responses: `200`, `422`

## `POST /api/v1/sessions`
Start Session
Request body: `SessionStart`
Responses: `201`, `422`

## `DELETE /api/v1/sessions/{session_id}`
Delete Session
Parameters: `session_id` (path, required), `confirm` (query)
Responses: `204`, `422`

## `GET /api/v1/sessions/{session_id}`
Get Session
Parameters: `session_id` (path, required)
Responses: `200`, `422`

## `POST /api/v1/sessions/{session_id}/end`
End Session
Parameters: `session_id` (path, required)
Request body: `SessionEnd`
Responses: `200`, `422`

## `POST /api/v1/sessions/{session_id}/extract`
Extract
Parameters: `session_id` (path, required)
Responses: `200`, `422`

## `POST /api/v1/sessions/{session_id}/prompts`
Add Prompt
Parameters: `session_id` (path, required)
Request body: `TextEntry`
Responses: `201`, `422`

## `POST /api/v1/sessions/{session_id}/responses`
Add Response
Parameters: `session_id` (path, required)
Request body: `TextEntry`
Responses: `201`, `422`

## `GET /api/v1/storage`
Storage
Responses: `200`

## `POST /api/v1/storage/gc`
Storage Gc
Responses: `200`

## `GET /api/v1/system/resources`
Resources
Responses: `200`

## `GET /api/v1/tasks`
List Tasks
Parameters: `limit` (query)
Responses: `200`, `422`

## `POST /api/v1/tasks`
Submit Task
Parameters: `x-pv-cloud-key` (header)
Request body: `TaskIn`
Responses: `202`, `422`

## `GET /api/v1/tasks/{task_id}`
Get Task
Parameters: `task_id` (path, required)
Responses: `200`, `422`

## `POST /api/v1/tasks/{task_id}/cancel`
Cancel Task
Parameters: `task_id` (path, required)
Responses: `200`, `422`

## `GET /api/v1/tasks/{task_id}/report`
Task Report
Parameters: `task_id` (path, required)
Responses: `200`, `422`

## `POST /api/v1/testing/run`
Run Tests
Request body: `TestRunIn`
Responses: `202`, `422`

## `GET /api/v1/testing/sessions`
Testing Sessions
Parameters: `project_id` (query), `agent` (query), `limit` (query)
Responses: `200`, `422`

## `GET /api/v1/testing/sessions/{session_id}`
Testing Session
Parameters: `session_id` (path, required)
Responses: `200`, `422`

## `GET /api/v1/testing/summary`
Testing Summary
Parameters: `project_id` (query)
Responses: `200`, `422`
