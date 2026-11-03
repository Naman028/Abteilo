// Mirrors the backend's Pydantic response models.

export interface Workspace {
  department_id: string;
  code: string;
  name: string;
  template: string;
  modules: string[];
  permissions: string[];
}

export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  company_id: string;
  company_name: string;
  company_permissions: string[];
  workspaces: Workspace[];
  departments: { id: string; name: string }[];
}

export interface Project {
  id: string;
  department_id: string;
  code: string;
  name: string;
  description: string;
  status: string;
  open_tasks: number;
  blocked_tasks: number;
  updated_at: string;
}

export interface Task {
  id: string;
  project_id: string;
  title: string;
  status: string;
  assignee_id: string | null;
  assignee_name: string | null;
  assignee_active?: boolean | null;
  due_date: string | null;
  updated_at: string;
}

export interface BudgetLine {
  department_id: string;
  department_name: string;
  period: string;
  budget_cents: number;
  approved_cents: number;
  pending_cents: number;
  remaining_cents: number;
}

export interface PurchaseRequest {
  id: string;
  department_id: string;
  department_name: string;
  title: string;
  justification: string;
  amount_cents: number;
  period: string;
  status: string;
  requested_by: string;
  requester_name: string;
  decision_note: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface LeaveRequest {
  id: string;
  user_id: string;
  employee_name: string;
  start_date: string;
  end_date: string;
  days: string;
  status: string;
  created_at: string;
}

export interface LeaveSummary {
  year: number;
  days_total: string;
  days_approved: string;
  days_pending: string;
  days_remaining: string;
  requests: LeaveRequest[];
}

export interface AccessRequest {
  id: string;
  target_user_id: string;
  target_name: string;
  role_code: string;
  elevated: boolean;
  department_name: string | null;
  justification: string;
  status: string;
  proposer_name: string;
  created_at: string;
}

export interface Role {
  code: string;
  name: string;
  scope: "company" | "department";
  permissions: string[];
}

export interface Member {
  user_id: string;
  display_name: string;
  title: string | null;
}

export interface DocumentView {
  id: string;
  department_id: string;
  department_name: string;
  title: string;
  classification: string;
  current_version: number;
  updated_at: string;
  index_status: "pending" | "indexed" | "failed";
}

export interface SearchHit {
  chunk_id: string;
  document_id: string;
  document_title: string;
  department_name: string;
  version: number;
  content: string;
  match: "keyword" | "semantic" | "both";
  score: number;
  keyword_rank: number | null;
  vector_rank: number | null;
  distance: number | null;
}

export interface SearchResult {
  hits: SearchHit[];
  mode: "hybrid" | "keyword" | "vector";
  degraded: string | null;
  embedding_model: string | null;
}

export interface DocumentContent extends DocumentView {
  content: string;
  content_sha256: string;
}

export interface Claim {
  text: string;
  evidence_ids: string[];
  status: ClaimStatus;
  reason: string | null;
  conflicts: string[];
  type?: "current_state" | "quantity" | "relationship" | "causal" | "policy" | "narrative";
}

export type Outcome = "answered" | "partial" | "insufficient" | "conflicting";

export type ClaimStatus = "verified" | "cited" | "outdated" | "conflicting" | "unsupported";

export interface Evidence {
  id: string;
  kind: string;
  as_of: string | null;
  title: string;
  source_type: string;
  source_id: string | null;
  content: string | null;
  freshness?: "live" | "current_version" | "projection" | "projection_stale";
}

export interface ReportRef {
  id: string;
  title: string;
  kind: string;
  complete: boolean;
  format: "pdf" | "docx";
  expires_at: string;
}

export interface AssistantPayload {
  summary: string;
  summary_status: ClaimStatus;
  summary_reason: string | null;
  claims: Claim[];
  evidence: Evidence[];
  status: "verified" | "partially_verified" | "insufficient_evidence";
  outcome?: Outcome;
  missing?: string[];
  degradations?: { source: string; code: string }[];
  follow_up: string | null;
  model_profile: string;
  tools_used: string[];
  timing?: { first_visible_ms: number | null; total_ms: number };
  report?: ReportRef | null;
}

export interface AssistantReply extends AssistantPayload {
  conversation_id: string;
  message_id: string;
}

export interface Conversation {
  id: string;
  department_id: string | null;
  title: string;
  last_message_at: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  payload: Partial<AssistantPayload>;
  created_at: string;
}

export type GraphKind = "department" | "project" | "task" | "person" | "purchase_request" | "leave_request" | "access_request" | "document";

export interface GraphNode {
  id: string;
  kind: GraphKind;
  ref_id: string;
  label: string;
  status: string | null;
  depth: number;
}

export interface GraphEvidence {
  source_type: string;
  source_id: string;
  document_version_id: string | null;
  quote: string | null;
}

export interface GraphEdge {
  id: string;
  src_id: string;
  dst_id: string;
  kind: string;
  authority: "record" | "document_mention" | "reviewed";
  valid_from: string;
  evidence: GraphEvidence[];
}

export interface Neighbourhood {
  root_id: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  projected_at: string | null;
}

export interface GraphCandidate {
  id: string;
  document_id: string;
  document_title: string;
  version: number;
  source: { id: string; kind: GraphKind; label: string };
  target: { id: string; kind: GraphKind; label: string };
  kind: "depends_on" | "blocked_by" | "delays" | "affects";
  quote: string;
  extractor: string;
  created_at: string;
}

export interface GraphStatus {
  last_started_at: string | null;
  last_finished_at: string | null;
  last_status: "ok" | "failed" | null;
  last_error_code: string | null;
  runs: number;
  entities: Record<string, number>;
  relationships: Record<string, number>;
  ended_relationships: number;
  candidates: Record<string, number>;
  extraction_failed: number;
  extraction_errors: Record<string, number>;
}
